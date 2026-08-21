package ai.omnigent.android

import android.content.DialogInterface
import android.os.Looper
import android.webkit.WebView
import androidx.appcompat.app.AlertDialog
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
import org.robolectric.shadows.ShadowDialog

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkConsentTest {
    private val hex = TEST_CONVERSATION_ID

    private fun store() = testStore()

    private fun latestDialog(): AlertDialog = ShadowDialog.getLatestDialog() as AlertDialog

    // AlertDialog's button click listeners post dismiss work to the main
    // looper; Robolectric needs an explicit pump for it (and any deep-link
    // follow-up work it schedules) to actually run before assertions.
    private fun idle() = shadowOf(Looper.getMainLooper()).idle()

    private fun launchWithLink(link: String): MainActivity {
        store().connect("https://current.example")
        return Robolectric
            .buildActivity(MainActivity::class.java, viewIntent(link))
            .setup()
            .get()
    }

    @Test
    fun `unknown server shows consent and does nothing until answered`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        assertTrue(latestDialog().isShowing)
        // Nothing loaded, pinned, or persisted pre-consent.
        assertEquals("https://current.example", activity.privateField("pinnedOrigin"))
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }

    @Test
    fun `consent open loads the origin but persists only on page ready`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()

        assertEquals("https://new.example", activity.privateField("pinnedOrigin"))
        assertEquals("https://new.example", shadowOf(activity.testWebView()).lastLoadedUrl)
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
        // Not yet a trusted recent: the load hasn't succeeded.
        assertFalse(store().recentServers().any { it.contains("new.example") })
        assertEquals("https://current.example", store().currentServerUrl())

        // Simulate the first successful pinned-origin load.
        activity.invokeOnPageReady("https://new.example/", mainFrameLoadFailed = false)
        assertEquals("https://new.example", store().currentServerUrl())
        assertTrue(store().recentServers().contains("https://new.example"))
    }

    @Test
    fun `failed consented load resolves navigation but persists after a successful retry`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))

        // WebView calls onPageFinished with the ORIGINAL url after a main-frame
        // load error (e.g. net::ERR_SSL_PROTOCOL_ERROR) — never a
        // chrome-error:// one — so this is the only way the shell finds out.
        activity.invokeOnPageReady("https://new.example/", mainFrameLoadFailed = true)

        // Never became a trusted recent, and the failed activation no longer
        // blocks later links. Persistence remains deferred for a successful retry.
        assertEquals("https://current.example", store().currentServerUrl())
        assertFalse(store().recentServers().any { it.contains("new.example") })
        assertNull(activity.privateField("pendingNavigatePath"))
        assertEquals("https://new.example", activity.privateField("pendingPersistUrl"))
        assertFalse(activity.privateField("pageLoaded") as Boolean)
        assertFalse(activity.privateField("processingDeepLink") as Boolean)

        activity.invokeOnPageReady("https://new.example/", mainFrameLoadFailed = false)
        assertNull(activity.privateField("pendingNavigatePath"))
        assertEquals("https://new.example", store().currentServerUrl())
        assertTrue(store().recentServers().contains("https://new.example"))
    }

    @Test
    fun `main frame HTTP error resolves activation and retains persistence until retry`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()

        activity.invokeOnPageReady(
            "https://new.example/",
            mainFramePersistenceFailed = true,
        )

        assertEquals("https://current.example", store().currentServerUrl())
        assertFalse(store().recentServers().any { it.contains("new.example") })
        assertNull(activity.privateField("pendingNavigatePath"))
        assertEquals("https://new.example", activity.privateField("pendingPersistUrl"))
        assertFalse(activity.privateField("processingDeepLink") as Boolean)

        activity.invokeOnPageReady("https://new.example/")

        assertNull(activity.privateField("pendingNavigatePath"))
        assertEquals("https://new.example", store().currentServerUrl())
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `queued link is processed after the head load permanently fails`() {
        val firstId = "first-$hex"
        val secondId = "second-$hex"
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://failed.example/c/$firstId"),
            )
        val activity = controller.setup().get()
        controller.newIntent(viewIntent("omnigent://next.example/c/$secondId"))
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()

        activity.invokeOnPageReady("https://failed.example/", mainFrameLoadFailed = true)

        assertFalse(activity.privateField("deepLinkAwaitingNavigation") as Boolean)
        assertTrue(latestDialog().isShowing)
        assertTrue((activity.privateField("deepLinkQueue") as ArrayDeque<*>).isEmpty())
    }

    @Test
    fun `consent cancel drops the link`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
        idle()

        assertEquals("https://current.example", activity.privateField("pinnedOrigin"))
        assertNull(activity.privateField("pendingNavigatePath"))
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }

    @Test
    fun `cold start with no server and unknown link consents instead of redirecting`() {
        // No store().connect — nothing configured.
        val activity =
            Robolectric
                .buildActivity(
                    MainActivity::class.java,
                    viewIntent("omnigent://new.example/c/$hex"),
                ).setup()
                .get()
        assertFalse(activity.isFinishing)
        assertNotNull(ShadowDialog.getLatestDialog())
    }

    @Test
    fun `ordinary intent cannot revert a consented transition before persistence`() {
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://new.example/c/$hex"),
            )
        store().connect("https://current.example")
        val activity = controller.setup().get()
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()

        controller.newIntent(android.content.Intent())

        assertEquals("https://new.example", activity.privateField("pinnedOrigin"))
        assertEquals("https://new.example", activity.privateField("pendingPersistUrl"))
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
    }

    @Test
    fun `ordinary intent cannot redirect a serverless consented cold start`() {
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://new.example/c/$hex"),
            )
        val activity = controller.setup().get()
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()

        controller.newIntent(android.content.Intent())

        assertEquals("https://new.example", activity.privateField("pinnedOrigin"))
        assertEquals("https://new.example", shadowOf(activity.testWebView()).lastLoadedUrl)
    }

    @Test
    fun `second link waits for the first consent (FIFO)`() {
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://first.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val first = latestDialog()
        controller.newIntent(viewIntent("omnigent://second.example/c/$hex"))
        // Still the first dialog; the second link is queued, not racing it.
        assertEquals(first, ShadowDialog.getLatestDialog())

        first.getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
        idle()
        // First resolved -> second dequeues and asks.
        val second = ShadowDialog.getLatestDialog() as AlertDialog
        assertTrue(second !== first && second.isShowing)
    }

    @Test
    fun `serverless decline stops queued consent work on the finishing activity`() {
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://first.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val first = latestDialog()
        controller.newIntent(viewIntent("omnigent://second.example/c/$hex"))

        first.getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
        idle()

        assertTrue(activity.isFinishing)
        assertEquals(first, ShadowDialog.getLatestDialog())
        assertNull(activity.privateField("deepLinkDialog"))
        assertTrue((activity.privateField("deepLinkQueue") as ArrayDeque<*>).isEmpty())
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `finishing before queued consent dequeues does not show another dialog`() {
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://first.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val first = latestDialog()
        controller.newIntent(viewIntent("omnigent://second.example/c/$hex"))
        activity.finish()

        first.getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
        idle()

        assertTrue(activity.isFinishing)
        assertEquals(first, ShadowDialog.getLatestDialog())
        assertNull(activity.privateField("deepLinkDialog"))
        assertTrue((activity.privateField("deepLinkQueue") as ArrayDeque<*>).isEmpty())
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `an exception mid-accept still resets processingDeepLink so later links aren't wedged`() {
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://new.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val realWebView = activity.testWebView()
        // Force reloadWithNewServer's webView.loadUrl to throw, simulating an
        // unexpected failure partway through accepting consent.
        val throwingWebView =
            object : WebView(activity) {
                override fun loadUrl(url: String): Unit = throw RuntimeException("boom")
            }
        activity.setPrivateField("webView", throwingWebView)

        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        try {
            // AlertDialog's button handler runs on the next looper pump, so
            // the exception surfaces here rather than from performClick().
            idle()
        } catch (_: RuntimeException) {
            // Expected: the queue must already be released by this point.
        }

        assertFalse(activity.privateField("processingDeepLink") as Boolean)

        // Restore a working WebView before continuing — the forced failure
        // above already proved its point; a real load must not also throw.
        activity.setPrivateField("webView", realWebView)

        // Queue isn't wedged: a later link still gets asked, not silently dropped.
        controller.newIntent(viewIntent("omnigent://another.example/c/$hex"))
        idle()
        val dialog = ShadowDialog.getLatestDialog() as AlertDialog
        assertTrue(dialog.isShowing)
    }

    @Test
    fun `accepted link path flushes before a queued same-origin path`() {
        val firstId = "first-$hex"
        val secondId = "second-$hex"
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://new.example/c/$firstId"),
            )
        val activity = controller.setup().get()
        controller.newIntent(viewIntent("omnigent://new.example/c/$secondId"))

        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()

        assertEquals("/c/$firstId", activity.privateField("pendingNavigatePath"))
        assertEquals(1, (activity.privateField("deepLinkQueue") as ArrayDeque<*>).size)

        activity.invokeOnPageReady("https://new.example/")

        assertNull(activity.privateField("pendingNavigatePath"))
        assertTrue((activity.privateField("deepLinkQueue") as ArrayDeque<*>).isEmpty())
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
        assertTrue(shadowOf(activity.testWebView()).lastEvaluatedJavascript.contains(secondId))
    }

    @Test
    fun `destroying the activity dismisses an open consent dialog without side effects`() {
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://new.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val dialog = latestDialog()
        assertTrue(dialog.isShowing)

        controller.destroy()

        assertFalse(dialog.isShowing)
        // Dismissal is not an accept: no persistence, no reload was triggered.
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }
}
