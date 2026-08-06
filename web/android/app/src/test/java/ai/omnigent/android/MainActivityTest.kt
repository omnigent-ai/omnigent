package ai.omnigent.android

import android.Manifest
import android.app.AlertDialog
import android.app.DownloadManager
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.ResolveInfo
import android.content.res.Configuration
import android.net.Uri
import android.os.Bundle
import android.os.Looper
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.RoboCookieManager
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import org.robolectric.annotation.RealObject
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowAlertDialog
import org.robolectric.shadows.ShadowCookieManager
import org.robolectric.shadows.ShadowDownloadManager
import org.robolectric.shadows.ShadowLog
import org.robolectric.shadows.ShadowLooper.idleMainLooper
import org.robolectric.shadows.ShadowNotificationManager
import org.robolectric.shadows.ShadowPopupMenu
import org.robolectric.shadows.ShadowToast
import org.robolectric.shadows.ShadowWebView
import org.robolectric.util.ReflectionHelpers
import org.robolectric.util.ReflectionHelpers.ClassParameter
import java.util.concurrent.AbstractExecutorService
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutorService
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], shadows = [CountingOmnigentWebViewClientShadow::class])
class MainActivityTest {
    @After
    fun resetCookieManager() {
        OidcLoginManager.resetProcessInstanceForTest()
        ShadowCookieManager.resetCookies()
        ShadowDownloadManager.reset()
        ShadowLog.clear()
    }

    @Test
    fun `renderer crashes recreate twice then user retry recreates from renderer failure`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val firstActivity = controller.get()
        val firstWebView = firstActivity.webView()

        assertTrue(
            firstActivity.shellWebViewClient().onRenderProcessGone(
                firstWebView,
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()
        val secondActivity = controller.get()
        val secondWebView = secondActivity.webView()

        assertTrue(firstActivity.isDestroyed)
        assertNotSame(firstActivity, secondActivity)
        assertNotSame(firstWebView, secondWebView)

        assertTrue(
            secondActivity.shellWebViewClient().onRenderProcessGone(
                secondWebView,
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()
        val thirdActivity = controller.get()
        val thirdWebView = thirdActivity.webView()
        val thirdAttachment = checkNotNull(thirdActivity.loginAttachment())

        assertTrue(secondActivity.isDestroyed)
        assertNotSame(secondActivity, thirdActivity)

        assertTrue(
            thirdActivity.shellWebViewClient().onRenderProcessGone(
                thirdWebView,
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()

        assertSame(thirdActivity, controller.get())
        assertFalse(thirdActivity.isDestroyed)
        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals(
            thirdActivity.getString(R.string.renderer_failed_title),
            shadowOf(dialog).title,
        )
        assertEquals(
            thirdActivity.getString(R.string.renderer_failed_body),
            shadowOf(dialog).message,
        )
        assertTrue(shadowOf(dialog).title != thirdActivity.getString(R.string.login_failed_title))
        assertNull(thirdActivity.loginFailedDialog())
        assertTrue(thirdActivity.webViewUnusable())
        assertNull(thirdActivity.loginAttachment())
        assertNull(thirdAttachment.callback)
        assertFalse(thirdActivity.loginManager().hasAttachmentForTest())
        assertNull(thirdWebView.parent)
        assertTrue(shadowOf(thirdWebView).wasDestroyCalled())

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
        idleMainLooper()

        assertTrue(thirdActivity.isDestroyed)
        assertNotSame(thirdActivity, controller.get())
    }

    @Test
    fun `reload recreates after the exhausted renderer failure is dismissed`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val deadWebView = activity.webView()
        ReflectionHelpers.setField(activity, "rendererRecreationAttempts", 2)

        assertTrue(
            activity.shellWebViewClient().onRenderProcessGone(
                deadWebView,
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()
        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        dialog.getButton(AlertDialog.BUTTON_NEGATIVE).performClick()
        idleMainLooper()

        activity.selectReloadMenuItem()
        idleMainLooper()

        assertTrue(activity.isDestroyed)
        assertNotSame(activity, controller.get())
        assertEquals(0, shadowOf(deadWebView).reloadInvocations)
    }

    @Test
    fun `server switch recreates and loads the persisted server after renderer exhaustion`() {
        val store = ServerStore(ApplicationProvider.getApplicationContext())
        store.connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        ReflectionHelpers.setField(activity, "rendererRecreationAttempts", 2)

        assertTrue(
            activity.shellWebViewClient().onRenderProcessGone(
                activity.webView(),
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()
        store.connect(NEW_SERVER_URL)

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)
        idleMainLooper()

        val recreatedActivity = controller.get()
        assertTrue(activity.isDestroyed)
        assertNotSame(activity, recreatedActivity)
        assertEquals(NEW_SERVER_URL, shadowOf(recreatedActivity.webView()).lastLoadedUrl)
    }

    @Test
    fun `direct server switch tears down login and notifications after renderer exhaustion`() {
        ShadowNotificationManager.reset()
        val store = ServerStore(ApplicationProvider.getApplicationContext())
        store.connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val (loginManager, releaseWorker) = activity.startBlockedLogin()

        try {
            activity.notifications().setBadgeCount(2, navigatePath = "/inbox")
            activity.notifications().notify("done", null, "/c/old")
            ReflectionHelpers.setField(activity, "rendererRecreationAttempts", 2)
            assertTrue(
                activity.shellWebViewClient().onRenderProcessGone(
                    activity.webView(),
                    renderProcessGoneDetail(),
                ),
            )
            idleMainLooper()
            assertTrue(loginManager.isInFlightForTest())
            assertEquals(2, activity.notificationManager().allNotifications.size)
            ReflectionHelpers.setField(activity, "pendingNavigatePath", "/c/old")
            store.connect(NEW_SERVER_URL)

            activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)
            idleMainLooper()

            assertFalse(loginManager.isInFlightForTest())
            assertTrue(activity.notificationManager().allNotifications.isEmpty())
            assertTrue(activity.isDestroyed)
            assertNull(controller.get().pendingNavigatePath())
            assertEquals(NEW_SERVER_URL, shadowOf(controller.get().webView()).lastLoadedUrl)
        } finally {
            releaseWorker.countDown()
        }
    }

    @Test
    fun `new intent server switch tears down login and notifications after renderer exhaustion`() {
        ShadowNotificationManager.reset()
        val store = ServerStore(ApplicationProvider.getApplicationContext())
        store.connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val (loginManager, releaseWorker) = activity.startBlockedLogin()

        try {
            activity.notifications().setBadgeCount(2, navigatePath = "/inbox")
            activity.notifications().notify("done", null, "/c/old")
            ReflectionHelpers.setField(activity, "rendererRecreationAttempts", 2)
            assertTrue(
                activity.shellWebViewClient().onRenderProcessGone(
                    activity.webView(),
                    renderProcessGoneDetail(),
                ),
            )
            idleMainLooper()
            assertTrue(loginManager.isInFlightForTest())
            assertEquals(2, activity.notificationManager().allNotifications.size)
            ReflectionHelpers.setField(activity, "pendingNavigatePath", "/c/old")
            store.connect(NEW_SERVER_URL)

            activity.invokeOnNewIntent(Intent(activity, MainActivity::class.java))
            idleMainLooper()

            assertFalse(loginManager.isInFlightForTest())
            assertTrue(activity.notificationManager().allNotifications.isEmpty())
            assertTrue(activity.isDestroyed)
            assertNull(controller.get().pendingNavigatePath())
            assertEquals(NEW_SERVER_URL, shadowOf(controller.get().webView()).lastLoadedUrl)
        } finally {
            releaseWorker.countDown()
        }
    }

    @Test
    fun `back exits without evaluating javascript after renderer exhaustion`() {
        val activity = activity()
        val deadWebView = activity.webView()
        ReflectionHelpers.setField(activity, "rendererRecreationAttempts", 2)

        assertTrue(
            activity.shellWebViewClient().onRenderProcessGone(
                deadWebView,
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()
        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        dialog.getButton(AlertDialog.BUTTON_NEGATIVE).performClick()
        idleMainLooper()
        val javascriptBeforeBack = shadowOf(deadWebView).lastEvaluatedJavascript

        activity.onBackPressedDispatcher.onBackPressed()

        assertTrue(activity.isFinishing)
        assertEquals(javascriptBeforeBack, shadowOf(deadWebView).lastEvaluatedJavascript)
    }

    @Test
    fun `renderer failure replaces a showing refusal dialog past the recreation cap`() {
        val activity = activity()
        val refusalDialog = activity.refuseEmbeddedSignIn()
        ReflectionHelpers.setField(activity, "rendererRecreationAttempts", 2)
        ActivityCallLog.clear()

        assertTrue(
            activity.shellWebViewClient().onRenderProcessGone(
                activity.webView(),
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()

        val rendererDialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        assertFalse(refusalDialog.isShowing)
        assertNull(activity.embeddedSignInDialog())
        assertTrue(rendererDialog.isShowing)
        assertEquals(
            activity.getString(R.string.renderer_failed_title),
            shadowOf(rendererDialog).title,
        )
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `successful pinned page resets the renderer recreation budget`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()

        repeat(2) {
            val activity = controller.get()
            assertTrue(
                activity.shellWebViewClient().onRenderProcessGone(
                    activity.webView(),
                    renderProcessGoneDetail(),
                ),
            )
            idleMainLooper()
        }

        val recoveredActivity = controller.get()
        recoveredActivity.invokeOnPageReady(PINNED_ORIGIN)
        assertTrue(
            recoveredActivity.shellWebViewClient().onRenderProcessGone(
                recoveredActivity.webView(),
                renderProcessGoneDetail(),
            ),
        )
        idleMainLooper()

        assertTrue(recoveredActivity.isDestroyed)
        assertNotSame(recoveredActivity, controller.get())
    }

    @Test
    fun `cutout-only safe area is published on every edge`() {
        val cutout = Insets.of(11, 23, 31, 0)
        val insets =
            WindowInsetsCompat
                .Builder()
                .setInsets(WindowInsetsCompat.Type.displayCutout(), cutout)
                .build()

        val safeArea = systemSafeAreaInsets(insets)
        assertEquals(cutout, safeArea)

        val script = androidSafeAreaScript(safeArea, 1f)
        assertTrue(script.contains("const top = '23.0px'"))
        assertTrue(script.contains("const left = '11.0px'"))
        assertTrue(script.contains("const right = '31.0px'"))
        assertTrue(script.contains("setProperty('--omnigent-safe-left', left)"))
        assertTrue(script.contains("setProperty('--omnigent-safe-right', right)"))
    }

    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.webView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        val darkConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(darkConfiguration)
        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)

        val lightConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_NO
            }
        activity.onConfigurationChanged(lightConfiguration)
        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `server switcher starts centered with a capped accessible label`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val density = activity.resources.displayMetrics.density
        val layout = button.layoutParams as FrameLayout.LayoutParams

        assertEquals(Gravity.TOP or Gravity.CENTER_HORIZONTAL, layout.gravity)
        assertEquals((172 * density).toInt(), button.maxWidth)
        assertEquals((48 * density).toInt(), button.minWidth)
        assertEquals(TextUtils.TruncateAt.MIDDLE, button.ellipsize)
        assertTrue(button.isSingleLine)
        assertEquals(host, button.contentDescription)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `server switcher band uses an absolute left margin`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.3, 0.8)
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)
        layout(parent, width = 1000, height = 600)
        // The width cap changes with the container, so the shared layout
        // listener repositions once more; a third pass applies it.
        layout(parent, width = 1000, height = 600)

        val layout = button.layoutParams as FrameLayout.LayoutParams
        // mdpi: the 48dp header-control reserve is 48px at each band edge.
        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, band, edgeReserve = 48)
        assertEquals(Gravity.TOP or Gravity.LEFT, layout.gravity)
        assertEquals(expectedLeft, layout.leftMargin)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)
    }

    @Test
    fun `server switcher hides beside a narrow right edge band`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.99, 1.0)
        layout(parent, width = 1000, height = 600)
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)

        val visibleWidth = minOf(button.right, parent.width) - maxOf(button.left, 0)
        assertEquals(View.INVISIBLE, button.visibility)
        assertTrue(button.width > serverSwitcherBandWidth(parent.width, band))
        assertTrue(button.left >= 0)
        assertTrue(button.right <= parent.width)
        assertEquals(button.width, visibleWidth)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `server switcher absolute left margin does not mirror in RTL`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.3, 0.8)
        parent.layoutDirection = View.LAYOUT_DIRECTION_RTL
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)
        layout(parent, width = 1000, height = 600)
        // Third pass: the container-driven width cap change repositions once more.
        layout(parent, width = 1000, height = 600)

        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, band, edgeReserve = 48)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `server switcher recentres when only its parent grows`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        // This resize keeps insets unchanged; avoid Robolectric redispatching them.
        ViewCompat.setOnApplyWindowInsetsListener(activity.webView(), null)
        val band = ServerSwitcherBand(0.0, 1.0)
        activity.setSwitcherBand(band)

        layout(parent, width = 400, height = 600)
        layout(parent, width = 400, height = 600)
        val initialWidth = button.width
        assertEquals(
            serverSwitcherLeftMargin(400, initialWidth, band, edgeReserve = 48),
            button.left,
        )
        button.removeLayoutChangeListeners()

        layout(parent, width = 700, height = 600)
        val params = button.layoutParams as FrameLayout.LayoutParams
        assertEquals(initialWidth, button.width)
        val grownLeft = serverSwitcherLeftMargin(700, initialWidth, band, edgeReserve = 48)
        assertEquals(grownLeft, params.leftMargin)

        layout(parent, width = 700, height = 600)
        assertEquals(grownLeft, button.left)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `redelivering the same band repositions after a parent resize`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.0, 1.0)
        activity.setSwitcherBand(band)

        layout(parent, width = 400, height = 600)
        layout(parent, width = 400, height = 600)
        val staleLeft = (button.layoutParams as FrameLayout.LayoutParams).leftMargin
        layout(parent, width = 700, height = 600)
        layout(parent, width = 700, height = 600)

        val params = button.layoutParams as FrameLayout.LayoutParams
        params.leftMargin = staleLeft
        button.layoutParams = params
        assertFalse(activity.isDestroyed)
        assertFalse(activity.isFinishing)
        val redeliveredBand = ServerSwitcherBand(0.0, 1.0)
        activity.receiveSwitcherBand(redeliveredBand)

        assertTrue(activity.switcherBand() === redeliveredBand)
        assertEquals(
            serverSwitcherLeftMargin(700, button.width, redeliveredBand, edgeReserve = 48),
            (button.layoutParams as FrameLayout.LayoutParams).leftMargin,
        )
    }

    @Test
    fun `pinned downloads use the native downloader and cross-origin downloads have no headers`() {
        ShadowDownloadManager.reset()
        val activity = activity()
        val webView = activity.webView()
        webView.settings.userAgentString = DOWNLOAD_USER_AGENT
        CookieManager.getInstance().setCookie(OFF_ORIGIN_DOWNLOAD_URL, OFF_ORIGIN_DOWNLOAD_COOKIE)
        val downloader = RecordingPinnedOriginDownloader()
        activity.replacePinnedOriginDownloader(downloader)
        val downloadManager =
            activity.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        val shadowDownloads = shadowOf(downloadManager)
        val listener = checkNotNull(shadowOf(webView).downloadListener)

        listener.onDownloadStart(
            PINNED_DOWNLOAD_URL,
            null,
            "attachment; filename=pinned.pdf",
            "application/pdf",
            1L,
        )
        listener.onDownloadStart(
            OFF_ORIGIN_DOWNLOAD_URL,
            null,
            "attachment; filename=foreign.pdf",
            "application/pdf",
            1L,
        )

        assertEquals(
            listOf(
                RecordedDownload(
                    PINNED_DOWNLOAD_URL,
                    PINNED_ORIGIN,
                    DOWNLOAD_USER_AGENT,
                    "application/pdf",
                    "pinned.pdf",
                ),
            ),
            downloader.downloads,
        )
        assertSame(activity.pinnedOrigin(), downloader.downloads.single().pinnedOrigin)
        val offOriginHeaders =
            shadowOf(checkNotNull(shadowDownloads.getRequest(0L)))
                .requestHeaders
                .associate { header -> header.first to header.second }
        assertFalse(offOriginHeaders.containsKey("Cookie"))
        assertFalse(offOriginHeaders.containsKey("User-Agent"))
        assertNull(shadowDownloads.getRequest(1L))
    }

    @Test
    @Config(shadows = [ThrowingDownloadManagerShadow::class])
    fun `cross-origin download enqueue failure is reported without crashing`() {
        val activity = activity()
        val listener = checkNotNull(shadowOf(activity.webView()).downloadListener)

        val result =
            runCatching {
                listener.onDownloadStart(
                    OFF_ORIGIN_DOWNLOAD_URL,
                    null,
                    "attachment; filename=foreign.pdf",
                    "application/pdf",
                    1L,
                )
            }
        idleMainLooper()

        assertTrue(result.isSuccess)
        assertEquals("Couldn't save foreign.pdf", ShadowToast.getTextOfLatestToast())
    }

    @Test
    fun `queued downloads are dropped after the WebView becomes unusable`() {
        val activity = activity()
        val webView = activity.webView()
        val listener = checkNotNull(shadowOf(webView).downloadListener)
        val downloader = RecordingPinnedOriginDownloader()
        activity.replacePinnedOriginDownloader(downloader)
        ReflectionHelpers.setField(activity, "webViewUnusable", true)

        listener.onDownloadStart(
            "blob:$PINNED_ORIGIN/blob-id",
            null,
            null,
            "application/octet-stream",
            1L,
        )
        listener.onDownloadStart(
            PINNED_DOWNLOAD_URL,
            null,
            "attachment; filename=pinned.pdf",
            "application/pdf",
            1L,
        )

        assertTrue(downloader.downloads.isEmpty())
        val downloadManager =
            activity.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        assertNull(shadowOf(downloadManager).getRequest(0L))
    }

    @Test
    fun `off-origin proxy auth page cannot open the file chooser`() {
        val activity = activity()
        val webView = activity.webView()
        activity.enterProxyAuth()
        webView.loadUrl(OFF_ORIGIN_AUTH_URL)
        ShadowLog.clear()
        var callbackCalls = 0
        var callbackValue: Array<Uri>? = arrayOf(Uri.EMPTY)

        val handled =
            checkNotNull(shadowOf(webView).webChromeClient).onShowFileChooser(
                webView,
                ValueCallback { value ->
                    callbackCalls++
                    callbackValue = value
                },
                fileChooserParams(),
            )

        assertTrue(handled)
        assertEquals(1, callbackCalls)
        assertNull(callbackValue)
        assertNull(activity.pendingFileCallback())
        assertTrue(
            ShadowLog
                .getLogsForTag("OmnigentAuth")
                .any { item -> item.msg.contains("file chooser denied") },
        )
    }

    @Test
    fun `file chooser is denied when no origin is pinned`() {
        val activity = activity()
        val webView = activity.webView()
        webView.loadUrl("about:blank")
        ReflectionHelpers.setField(activity, "pinnedOrigin", null)
        var callbackCalls = 0
        var callbackValue: Array<Uri>? = arrayOf(Uri.EMPTY)

        val handled =
            checkNotNull(shadowOf(webView).webChromeClient).onShowFileChooser(
                webView,
                ValueCallback { value ->
                    callbackCalls++
                    callbackValue = value
                },
                fileChooserParams(),
            )

        assertTrue(handled)
        assertEquals(1, callbackCalls)
        assertNull(callbackValue)
        assertNull(activity.pendingFileCallback())
    }

    @Test
    fun `null origin microphone request is denied when no origin is pinned`() {
        val activity = activity()
        shadowOf(activity).grantPermissions(Manifest.permission.RECORD_AUDIO)
        ReflectionHelpers.setField(activity, "pinnedOrigin", null)
        val request = RecordingPermissionRequest(null)

        checkNotNull(shadowOf(activity.webView()).webChromeClient).onPermissionRequest(request)

        assertEquals(1, request.denyCalls)
        assertEquals(0, request.grantCalls)
    }

    @Test
    fun `page readiness is ignored when no origin is pinned`() {
        val activity = activity()
        ReflectionHelpers.setField(activity, "pinnedOrigin", null)
        ReflectionHelpers.setField(activity, "loginAttempts", 2)

        activity.invokeOnPageReady("about:blank")

        assertFalse(activity.pageLoaded())
        assertEquals(2, activity.loginAttempts())
    }

    @Test
    fun `pending activation is retained when no origin is pinned`() {
        val activity = activity()
        activity.webView().loadUrl("about:blank")
        ReflectionHelpers.setField(activity, "pinnedOrigin", null)
        ReflectionHelpers.setField(activity, "pendingNavigatePath", "/c/pending")

        activity.invokeFlushPendingActivation()

        assertEquals("/c/pending", activity.pendingNavigatePath())
    }

    @Test
    fun `refusal shows the browser-required dialog`() {
        val activity = activity()

        val dialog = activity.refuseEmbeddedSignIn()
        val shadowDialog = shadowOf(dialog)

        assertEquals(activity.getString(R.string.proxy_auth_refused_title), shadowDialog.title)
        assertEquals(
            "This server's sign-in provider doesn't allow signing in inside an app. " +
                "Open example.com in your browser to continue. " +
                "Signing in there won't sign you in here.",
            shadowDialog.message,
        )
        assertEquals(
            activity.getString(R.string.proxy_auth_open_browser),
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).text,
        )
        assertEquals(
            activity.getString(R.string.proxy_auth_cancel),
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).text,
        )
        assertTrue(dialog.isShowing)
        assertEquals(ProxyAuthState.REFUSED, activity.shellWebViewClient().proxyAuthState())
    }

    @Test
    fun `only this app resolving reports no browser and keeps the dialog open`() {
        val activity = activity()
        activity.addBrowser(activity.packageName, "OwnBrowserActivity")
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_body),
            shadowOf(dialog).message,
        )
        assertTrue(dialog.isShowing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertEquals(0, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `one external browser launches as an explicit component`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
        idleMainLooper()

        val started = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(Intent.ACTION_VIEW, started.action)
        assertEquals(PINNED_ORIGIN, started.dataString)
        assertEquals(EXTERNAL_BROWSER_PACKAGE, started.component?.packageName)
        assertTrue(started.component?.packageName != activity.packageName)
        assertFalse(dialog.isShowing)
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `self and external browsers produce a chooser whose every intent excludes self`() {
        val activity = activity()
        activity.addBrowser(activity.packageName, "OwnBrowserActivity")
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        activity.addBrowser(SECOND_BROWSER_PACKAGE, SECOND_BROWSER_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        val chooser = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(Intent.ACTION_CHOOSER, chooser.action)
        val target =
            checkNotNull(
                chooser.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java),
            )
        val initial =
            checkNotNull(chooser.getParcelableArrayExtra(Intent.EXTRA_INITIAL_INTENTS))
                .map { it as Intent }
        val browserIntents = listOf(target) + initial
        assertEquals(
            setOf(EXTERNAL_BROWSER_PACKAGE, SECOND_BROWSER_PACKAGE),
            browserIntents.map { it.component?.packageName }.toSet(),
        )
        assertTrue(initial.isNotEmpty())
        assertTrue(
            browserIntents.all { intent ->
                intent.component != null &&
                    intent.component?.packageName != activity.packageName &&
                    intent.dataString == PINNED_ORIGIN
            },
        )
    }

    @Test
    fun `no resolvers changes the action to an acknowledgement that dismisses`() {
        val activity = activity()
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_body),
            shadowOf(dialog).message,
        )
        assertEquals(
            activity.getString(android.R.string.ok),
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).text,
        )
        assertTrue(dialog.isShowing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertEquals(0, ActivityCallLog.endProxyAuthCalls)

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
        idleMainLooper()

        assertFalse(dialog.isShowing)
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `every dialog dismissal route resets proxy auth exactly once`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        val dismissals =
            listOf<(AlertDialog) -> Unit>(
                { dialog -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick() },
                { dialog -> dialog.getButton(AlertDialog.BUTTON_NEGATIVE).performClick() },
                { dialog -> dialog.onBackPressed() },
                { dialog ->
                    assertTrue(shadowOf(dialog).isCancelableOnTouchOutside)
                    dialog.cancel()
                },
            )

        dismissals.forEachIndexed { index, dismiss ->
            val dialog = activity.refuseEmbeddedSignIn()

            dismiss(dialog)
            idleMainLooper()

            assertFalse(dialog.isShowing)
            assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
            assertEquals(index + 1, ActivityCallLog.endProxyAuthCalls)

            dialog.dismiss()
            idleMainLooper()
            assertEquals(index + 1, ActivityCallLog.endProxyAuthCalls)
        }
    }

    @Test
    fun `server reload dismisses the refusal dialog without double reset`() {
        val activity = activity()
        val dialog = activity.refuseEmbeddedSignIn()
        ActivityCallLog.clear()

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        assertFalse(dialog.isShowing)
        assertNull(activity.embeddedSignInDialog())
        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `server reload cancels notifications from the previous origin`() {
        ShadowNotificationManager.reset()
        val activity = activity()
        activity.notifications().setBadgeCount(2, navigatePath = "/inbox")
        activity.notifications().notify("done", null, "/c/old")
        assertEquals(2, activity.notificationManager().allNotifications.size)

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        assertTrue(activity.notificationManager().allNotifications.isEmpty())
    }

    @Test
    fun `server reload clears a pending notification activation`() {
        val activity = activity()
        ReflectionHelpers.setField(activity, "pendingNavigatePath", "/c/old")

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        assertNull(activity.pendingNavigatePath())
    }

    @Test
    fun `server reload pins future notification activations to the new origin`() {
        ShadowNotificationManager.reset()
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        activity.notifications().notify("done", null, "/c/new")

        val posted = activity.notificationManager().allNotifications.single()
        val intent = shadowOf(posted.contentIntent).savedIntent
        assertEquals(
            NEW_ORIGIN,
            intent.getStringExtra(NativeNotificationManager.EXTRA_NOTIFICATION_ORIGIN),
        )
    }

    @Test
    fun `notification activations without the pinned origin are dropped`() {
        val activity = activity()
        ReflectionHelpers.setField(activity, "pageLoaded", false)

        listOf(
            notificationIntent("/c/stale", NEW_ORIGIN),
            notificationIntent("/c/missing", null),
        ).forEach { intent ->
            activity.invokeOnNewIntent(intent)
            assertNull(activity.pendingNavigatePath())
        }
    }

    @Test
    fun `cold start pending notification activation survives recreate`() {
        val context: Context = ApplicationProvider.getApplicationContext()
        ServerStore(context).connect(PINNED_ORIGIN)
        val intent = notificationIntent("/c/cold", PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java, intent).setup()
        val firstActivity = controller.get()

        assertEquals("/c/cold", firstActivity.pendingNavigatePath())
        assertFalse(firstActivity.intent.hasExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH))
        firstActivity.recreate()
        idleMainLooper()

        assertEquals("/c/cold", controller.get().pendingNavigatePath())
    }

    @Test
    fun `new intent pending notification activation survives recreate`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val activityController = Robolectric.buildActivity(MainActivity::class.java).setup()
        val firstActivity = activityController.get()
        val intent = notificationIntent("/c/warm", PINNED_ORIGIN)
        ReflectionHelpers.setField(firstActivity, "pageLoaded", false)

        firstActivity.invokeOnNewIntent(intent)

        assertEquals("/c/warm", firstActivity.pendingNavigatePath())
        assertFalse(firstActivity.intent.hasExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH))
        firstActivity.recreate()
        idleMainLooper()

        assertEquals("/c/warm", activityController.get().pendingNavigatePath())
    }

    @Test
    fun `handled notification activation is not replayed after recreate`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val firstActivity = controller.get()
        val intent = notificationIntent("/c/handled", PINNED_ORIGIN)
        firstActivity.invokeOnPageReady(PINNED_ORIGIN)

        firstActivity.invokeOnNewIntent(intent)

        assertNull(firstActivity.pendingNavigatePath())
        assertTrue(
            checkNotNull(shadowOf(firstActivity.webView()).lastEvaluatedJavascript)
                .contains("/c/handled"),
        )
        assertFalse(firstActivity.intent.hasExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH))
        firstActivity.recreate()
        idleMainLooper()

        assertNull(controller.get().pendingNavigatePath())
    }

    @Test
    fun `restored notification activation takes precedence over the launch intent`() {
        val context: Context = ApplicationProvider.getApplicationContext()
        ServerStore(context).connect(PINNED_ORIGIN)
        val savedState =
            Bundle().apply {
                putString("pendingNavigatePath", "/c/restored")
                putString("pendingNavigateOrigin", PINNED_ORIGIN)
            }
        val launchIntent = notificationIntent("/c/launch", PINNED_ORIGIN)

        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java, launchIntent)
                .setup(savedState)
                .get()

        assertEquals("/c/restored", activity.pendingNavigatePath())
    }

    @Test
    fun `restored notification activation rejects a path without a leading slash`() {
        val context: Context = ApplicationProvider.getApplicationContext()
        ServerStore(context).connect(PINNED_ORIGIN)
        val savedState =
            Bundle().apply {
                putString("pendingNavigatePath", "https://evil.example/capture")
                putString("pendingNavigateOrigin", PINNED_ORIGIN)
            }

        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java)
                .setup(savedState)
                .get()

        assertNull(activity.pendingNavigatePath())
    }

    @Test
    fun `notification activation fails closed when no expected origin exists`() {
        val activity = activity()
        val intent = notificationIntent("/c/untrusted", null)

        val path = activity.takeNavigatePathOf(intent, null)

        assertNull(path)
        assertFalse(intent.hasExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH))
    }

    @Test
    fun `saved state without an activation does not replay the launch intent`() {
        val context: Context = ApplicationProvider.getApplicationContext()
        ServerStore(context).connect(PINNED_ORIGIN)
        val launchIntent = notificationIntent("/c/already-handled", PINNED_ORIGIN)

        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java, launchIntent)
                .setup(Bundle())
                .get()

        assertNull(activity.pendingNavigatePath())
    }

    @Test
    fun `restored notification activation is dropped after the pinned origin changes`() {
        val context: Context = ApplicationProvider.getApplicationContext()
        ServerStore(context).connect(NEW_SERVER_URL)
        val savedState =
            Bundle().apply {
                putString("pendingNavigatePath", "/c/old-server")
                putString("pendingNavigateOrigin", PINNED_ORIGIN)
            }

        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java)
                .setup(savedState)
                .get()

        assertNull(activity.pendingNavigatePath())
    }

    @Test
    fun `fresh notification intent survives recreation of an unusable WebView`() {
        val context: Context = ApplicationProvider.getApplicationContext()
        ServerStore(context).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val firstActivity = controller.get()
        ReflectionHelpers.setField(firstActivity, "webViewUnusable", true)

        firstActivity.invokeOnNewIntent(notificationIntent("/c/fresh", PINNED_ORIGIN))
        idleMainLooper()

        assertTrue(firstActivity.isDestroyed)
        assertEquals("/c/fresh", controller.get().pendingNavigatePath())
    }

    @Test
    fun `onDestroy removes the bridge before shutting down the blob saver`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val saver = activity.blobSaver()
        val originalExecutor: ExecutorService = ReflectionHelpers.getField(saver, "io")
        originalExecutor.shutdown()
        var bridgeInstalledAtShutdown: Boolean? = null
        ReflectionHelpers.setField(
            saver,
            "io",
            RecordingShutdownExecutor {
                bridgeInstalledAtShutdown =
                    ReflectionHelpers.getField(activity, "bridgeTransportInstalled")
            },
        )
        ReflectionHelpers.setField(activity, "bridgeTransportInstalled", true)

        controller.destroy()

        assertEquals(false, bridgeInstalledAtShutdown)
    }

    @Test
    fun `recreation keeps the process login manager and replaces its attachment`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val firstActivity = controller.get()
        val manager = firstActivity.loginManager()
        val firstAttachment = checkNotNull(firstActivity.loginAttachment())

        firstActivity.recreate()
        idleMainLooper()

        val recreatedActivity = controller.get()
        assertTrue(firstActivity.isDestroyed)
        assertSame(manager, recreatedActivity.loginManager())
        assertFalse(manager.isShutDownForTest())
        assertNull(firstActivity.loginAttachment())
        assertNotSame(firstAttachment, checkNotNull(recreatedActivity.loginAttachment()))

        controller.destroy()

        assertFalse(manager.hasAttachmentForTest())
        assertFalse(manager.isShutDownForTest())
    }

    @Test
    fun `server reload cancels an in-flight native login`() {
        val activity = activity()
        val loginManager = activity.loginManager()
        val executor: ExecutorService = ReflectionHelpers.getField(loginManager, "io")
        val workerStarted = CountDownLatch(1)
        val holdWorker = CountDownLatch(1)
        executor.execute {
            workerStarted.countDown()
            runCatching { holdWorker.await() }
        }
        assertTrue(workerStarted.await(5, TimeUnit.SECONDS))

        try {
            assertTrue(loginManager.start(activity, PINNED_ORIGIN))
            assertTrue(loginManager.isInFlightForTest())

            activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

            assertFalse(loginManager.isInFlightForTest())
        } finally {
            holdWorker.countDown()
            loginManager.shutdown()
        }
    }

    @Test
    fun `leaving the app for good abandons an in-flight login`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val loginManager = activity.loginManager()
        val executor: ExecutorService = ReflectionHelpers.getField(loginManager, "io")
        val workerStarted = CountDownLatch(1)
        val holdWorker = CountDownLatch(1)
        executor.execute {
            workerStarted.countDown()
            runCatching { holdWorker.await() }
        }
        assertTrue(workerStarted.await(5, TimeUnit.SECONDS))

        try {
            assertTrue(loginManager.start(activity, PINNED_ORIGIN))
            assertTrue(loginManager.isInFlightForTest())

            activity.finish()
            controller.destroy()

            // Finishing abandons process-scoped work instead of polling after exit.
            assertFalse(loginManager.isInFlightForTest())
            assertFalse(loginManager.hasHeldResultForTest())
        } finally {
            holdWorker.countDown()
        }
    }

    @Test
    fun `recreation leaves an in-flight login polling`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val loginManager = activity.loginManager()
        val executor: ExecutorService = ReflectionHelpers.getField(loginManager, "io")
        val workerStarted = CountDownLatch(1)
        val holdWorker = CountDownLatch(1)
        executor.execute {
            workerStarted.countDown()
            runCatching { holdWorker.await() }
        }
        assertTrue(workerStarted.await(5, TimeUnit.SECONDS))

        try {
            assertTrue(loginManager.start(activity, PINNED_ORIGIN))
            assertTrue(loginManager.isInFlightForTest())

            activity.recreate()
            idleMainLooper()

            // The whole point of process-scoping: a browser login finished during a
            // renderer-crash recovery must still land.
            assertTrue(loginManager.isInFlightForTest())
        } finally {
            holdWorker.countDown()
        }
    }

    @Test
    fun `server reload rebinds login result delivery to the new origin`() {
        val activity = activity()
        val firstAttachment = checkNotNull(activity.loginAttachment())

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        val reboundAttachment = checkNotNull(activity.loginAttachment())
        assertNotSame(firstAttachment, reboundAttachment)
        assertNull(firstAttachment.callback)
        assertEquals(NEW_ORIGIN, reboundAttachment.origin)
    }

    @Test
    fun `refusal while finishing resets to idle without showing a dialog`() {
        val activity = activity()
        activity.finish()

        activity.enterProxyAuth()
        activity.shellWebViewClient().onPageStarted(activity.webView(), REFUSAL_URL, null)

        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `menu escape hatch ends an in-flight flow and opens externally`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        activity.enterProxyAuth()
        assertEquals(ProxyAuthState.IN_FLIGHT, activity.shellWebViewClient().proxyAuthState())

        activity.selectOpenInBrowserMenuItem()

        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
        val started = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(EXTERNAL_BROWSER_PACKAGE, started.component?.packageName)
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
    }

    @Test
    fun `menu escape hatch dismisses refusal and resets exactly once`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()
        ActivityCallLog.clear()

        activity.selectOpenInBrowserMenuItem()
        idleMainLooper()

        assertFalse(dialog.isShowing)
        assertNull(activity.embeddedSignInDialog())
        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
        val started = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(EXTERNAL_BROWSER_PACKAGE, started.component?.packageName)
    }

    @Test
    fun `menu escape hatch with no browsers shows a toast without a dialog`() {
        val activity = activity()

        activity.selectOpenInBrowserMenuItem()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_toast),
            ShadowToast.getTextOfLatestToast(),
        )
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `rejected login shows generic retry dialog and never the refusal dialog`() {
        val activity = activity()

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)
        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        val shadowDialog = shadowOf(dialog)

        assertEquals(
            activity.getString(R.string.login_failed_title),
            shadowDialog.title,
        )
        assertEquals(
            activity.getString(R.string.login_failed_body, "example.com"),
            shadowDialog.message,
        )
        assertTrue(shadowDialog.title != activity.getString(R.string.proxy_auth_refused_title))
        assertEquals(
            activity.getString(R.string.login_failed_retry),
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).text,
        )
        assertEquals(
            activity.getString(R.string.proxy_auth_cancel),
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).text,
        )
    }

    @Test
    fun `timed out login shows the generic retry dialog`() {
        val activity = activity()

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.TimedOut)
        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())

        assertEquals(activity.getString(R.string.login_failed_title), shadowOf(dialog).title)
        assertEquals(
            activity.getString(R.string.login_failed_body, "example.com"),
            shadowOf(dialog).message,
        )
    }

    @Test
    fun `login failures do not stack a generic dialog over the refusal dialog`() {
        val activity = activity()
        val refusalDialog = activity.refuseEmbeddedSignIn()

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)
        activity.onLoginResult(PINNED_ORIGIN, LoginResult.TimedOut)

        assertSame(refusalDialog, ShadowAlertDialog.getLatestAlertDialog())
        assertTrue(refusalDialog.isShowing)
        assertNull(activity.loginFailedDialog())
    }

    @Test
    fun `repeated login failure reuses the generic dialog`() {
        val activity = activity()
        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)
        val firstDialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.TimedOut)

        assertSame(firstDialog, ShadowAlertDialog.getLatestAlertDialog())
        assertSame(firstDialog, activity.loginFailedDialog())
        assertTrue(firstDialog.isShowing)
    }

    @Test
    fun `exhausted login budget shows retry and retry starts a fresh attempt`() {
        val activity = activity()
        val loginManager = activity.loginManager()
        val executor: ExecutorService = ReflectionHelpers.getField(loginManager, "io")
        val workerStarted = CountDownLatch(1)
        val holdWorker = CountDownLatch(1)
        executor.execute {
            workerStarted.countDown()
            runCatching { holdWorker.await() }
        }
        assertTrue(workerStarted.await(5, TimeUnit.SECONDS))

        try {
            ReflectionHelpers.setField(activity, "loginAttempts", 3)
            activity.invokeStartLogin()
            val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
            assertEquals(
                activity.getString(R.string.login_failed_body, "example.com"),
                shadowOf(dialog).message,
            )

            dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
            idleMainLooper()

            assertEquals(1, activity.loginAttempts())
        } finally {
            loginManager.shutdown()
            holdWorker.countDown()
        }
    }

    @Test
    fun `non JWT session token shows the generic retry dialog`() {
        val activity = activity()

        activity.invokeOnSessionToken("not-a-jwt")

        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals(activity.getString(R.string.login_failed_title), shadowOf(dialog).title)
        assertEquals(
            activity.getString(R.string.login_failed_body, "example.com"),
            shadowOf(dialog).message,
        )
    }

    @Test
    fun `rejected session cookie shows failure without announcing sign in`() {
        ShadowNotificationManager.reset()
        ReflectionHelpers.setStaticField(
            ShadowCookieManager::class.java,
            "cookieManager",
            RejectingCookieManager(),
        )
        val activity = activity()

        activity.invokeOnSessionToken(SESSION_TOKEN)

        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals(activity.getString(R.string.login_failed_title), shadowOf(dialog).title)
        assertEquals(
            activity.getString(R.string.login_failed_body, "example.com"),
            shadowOf(dialog).message,
        )
        assertTrue(activity.notificationManager().allNotifications.isEmpty())
        assertNull(shadowOf(activity).peekNextStartedActivity())
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `accepted secure session cookie is hardened then reloads and announces sign in`() {
        ShadowNotificationManager.reset()
        val cookieManager = RecordingCookieManager(accepted = true)
        ReflectionHelpers.setStaticField(
            ShadowCookieManager::class.java,
            "cookieManager",
            cookieManager,
        )
        val activity = activity()

        activity.invokeOnSessionToken(SESSION_TOKEN)

        assertEquals(PINNED_ORIGIN, cookieManager.writtenUrl)
        assertTrue(checkNotNull(cookieManager.writtenCookie).contains("; HttpOnly"))
        assertTrue(checkNotNull(cookieManager.writtenCookie).contains("; Secure"))
        assertTrue(checkNotNull(cookieManager.writtenCookie).contains("; SameSite=Lax"))
        assertTrue(ActivityCallLog.entries.contains("loadUrl:$PINNED_ORIGIN"))
        val notification = activity.notificationManager().allNotifications.single()
        assertEquals(
            activity.getString(R.string.signed_in_title),
            notification.extras.getCharSequence(Notification.EXTRA_TITLE),
        )
        val reorderIntent = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(MainActivity::class.java.name, reorderIntent.component?.className)
        assertTrue(reorderIntent.flags and Intent.FLAG_ACTIVITY_REORDER_TO_FRONT != 0)
    }

    @Test
    fun `http session cookie is HttpOnly without Secure`() {
        val cookieManager = RecordingCookieManager(accepted = null)
        ReflectionHelpers.setStaticField(
            ShadowCookieManager::class.java,
            "cookieManager",
            cookieManager,
        )
        val activity = activity()

        activity.invokeOnSessionToken(SESSION_TOKEN, "http://example.com")

        val cookie = checkNotNull(cookieManager.writtenCookie)
        assertTrue(cookie.contains("; HttpOnly"))
        assertTrue(cookie.contains("; SameSite=Lax"))
        assertFalse(cookie.contains("; Secure"))
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `session cookie callback is dropped while activity is finishing`() {
        ShadowNotificationManager.reset()
        val activity = activity()
        activity.finish()
        ActivityCallLog.clear()

        activity.onSessionCookieSet(PINNED_ORIGIN, accepted = true)

        assertTrue(ActivityCallLog.entries.isEmpty())
        assertTrue(activity.notificationManager().allNotifications.isEmpty())
        assertNull(shadowOf(activity).peekNextStartedActivity())
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `success from the previous origin sets no cookie and loads nothing`() {
        ShadowCookieManager.resetCookies()
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)
        ActivityCallLog.clear()

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Success(SESSION_TOKEN))

        assertNull(CookieManager.getInstance().getCookie(PINNED_ORIGIN))
        assertNull(CookieManager.getInstance().getCookie(NEW_ORIGIN))
        assertTrue(ActivityCallLog.entries.isEmpty())
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `a server switch before the cookie callback cancels the reload`() {
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)
        ActivityCallLog.clear()

        // setCookie acknowledges asynchronously: a switch landing between the
        // write and its callback must not reload the previous server.
        activity.onSessionCookieSet(PINNED_ORIGIN, accepted = true)

        assertTrue(ActivityCallLog.entries.none { it.startsWith("loadUrl:") })
    }

    @Test
    fun `rejection from the previous origin shows no failure surface`() {
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)

        assertNull(activity.loginFailedDialog())
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
    }

    @Test
    fun `timeout from the previous origin shows no failure surface`() {
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.TimedOut)

        assertNull(activity.loginFailedDialog())
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
    }

    @Test
    fun `browsable host deep-link handler is not treated as a browser`() {
        val activity = activity()
        val deepLinkIntent =
            Intent(Intent.ACTION_VIEW, Uri.parse(PINNED_ORIGIN))
                .addCategory(Intent.CATEGORY_BROWSABLE)
        activity.addResolver(deepLinkIntent, DEEP_LINK_PACKAGE, DEEP_LINK_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_body),
            shadowOf(dialog).message,
        )
        assertTrue(dialog.isShowing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `server reload calls reset before ledgered stop before loadUrl`() {
        val activity = activity()
        val client = activity.shellWebViewClient()
        val webView = activity.webView()
        client.onPageStarted(webView, OLD_LOADING_URL, null)

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        assertEquals(
            listOf(
                "endProxyAuth",
                "stopLoading",
                "loadUrl:$NEW_SERVER_URL",
            ),
            ActivityCallLog.entries,
        )
        assertEquals(OLD_LOADING_URL, client.lastSelfStoppedUrl())
    }

    private fun activity(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        while (shadowOf(activity).nextStartedActivity != null) {
            // Drain setup-only launches before testing browser dispatch.
        }
        ActivityCallLog.clear()
        return activity
    }

    private fun MainActivity.refuseEmbeddedSignIn(): AlertDialog {
        enterProxyAuth()
        shellWebViewClient().onPageStarted(webView(), REFUSAL_URL, null)
        return checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
    }

    private fun MainActivity.enterProxyAuth(origin: String = PINNED_ORIGIN) {
        assertFalse(
            shellWebViewClient().shouldOverrideUrlLoading(
                webView(),
                request(proxyAuthUrl(origin), redirect = true),
            ),
        )
    }

    private fun MainActivity.addBrowser(
        resolverPackage: String,
        resolverActivity: String,
    ) {
        addResolver(browserProbe(), resolverPackage, resolverActivity)
    }

    private fun MainActivity.addResolver(
        intent: Intent,
        resolverPackage: String,
        resolverActivity: String,
    ) {
        val resolveInfo =
            ResolveInfo().apply {
                activityInfo =
                    ActivityInfo().apply {
                        packageName = resolverPackage
                        name = resolverActivity
                        exported = true
                    }
                isDefault = true
            }
        shadowOf(packageManager).addResolveInfoForIntent(intent, resolveInfo)
    }

    private fun MainActivity.selectOpenInBrowserMenuItem() {
        switchButton().performClick()
        val popup = checkNotNull(ShadowPopupMenu.getLatestPopupMenu())
        val expectedTitle = getString(R.string.menu_open_in_browser)
        val item =
            (0 until popup.menu.size())
                .map(popup.menu::getItem)
                .single { it.title.toString() == expectedTitle }
        assertTrue(checkNotNull(shadowOf(popup).onMenuItemClickListener).onMenuItemClick(item))
    }

    private fun MainActivity.selectReloadMenuItem() {
        switchButton().performClick()
        val popup = checkNotNull(ShadowPopupMenu.getLatestPopupMenu())
        val expectedTitle = getString(R.string.menu_reload)
        val item =
            (0 until popup.menu.size())
                .map(popup.menu::getItem)
                .single { it.title.toString() == expectedTitle }
        assertTrue(checkNotNull(shadowOf(popup).onMenuItemClickListener).onMenuItemClick(item))
    }

    private fun MainActivity.reloadWithNewServer(
        serverUrl: String,
        newOrigin: String,
    ) {
        ReflectionHelpers.callInstanceMethod<Unit>(
            this,
            "reloadWithNewServer",
            ClassParameter.from(String::class.java, serverUrl),
            ClassParameter.from(String::class.java, newOrigin),
        )
    }

    private fun MainActivity.shellWebViewClient(): OmnigentWebViewClient =
        ReflectionHelpers.getField(this, "shellWebViewClient")

    private fun MainActivity.embeddedSignInDialog(): AlertDialog? =
        ReflectionHelpers.getField(this, "embeddedSignInDialog")

    private fun MainActivity.loginFailedDialog(): AlertDialog? =
        ReflectionHelpers.getField(this, "loginFailedDialog")

    private fun MainActivity.loginAttempts(): Int =
        ReflectionHelpers.getField(this, "loginAttempts")

    private fun MainActivity.pageLoaded(): Boolean = ReflectionHelpers.getField(this, "pageLoaded")

    private fun MainActivity.pinnedOrigin(): String? =
        ReflectionHelpers.getField(this, "pinnedOrigin")

    private fun MainActivity.loginManager(): OidcLoginManager =
        ReflectionHelpers.getField(this, "loginManager")

    private fun MainActivity.loginAttachment(): OidcLoginManager.Attachment? =
        ReflectionHelpers.getField(this, "loginAttachment")

    private fun MainActivity.notifications(): NativeNotificationManager =
        ReflectionHelpers.getField(this, "notifications")

    private fun MainActivity.blobSaver(): BlobSaver = ReflectionHelpers.getField(this, "blobSaver")

    private fun MainActivity.replacePinnedOriginDownloader(
        replacement: PinnedOriginDownloadHandler,
    ) {
        val original: PinnedOriginDownloadHandler =
            ReflectionHelpers.getField(this, "pinnedOriginDownloader")
        original.shutdown()
        ReflectionHelpers.setField(this, "pinnedOriginDownloader", replacement)
    }

    private fun MainActivity.pendingNavigatePath(): String? =
        ReflectionHelpers.getField(this, "pendingNavigatePath")

    private fun MainActivity.takeNavigatePathOf(
        intent: Intent,
        expectedOrigin: String?,
    ): String? =
        ReflectionHelpers.callInstanceMethod(
            this,
            "takeNavigatePathOf",
            ClassParameter.from(Intent::class.java, intent),
            ClassParameter.from(String::class.java, expectedOrigin),
        )

    private fun MainActivity.pendingFileCallback(): ValueCallback<Array<Uri>>? =
        ReflectionHelpers.getField(this, "pendingFileCallback")

    private fun MainActivity.webViewUnusable(): Boolean =
        ReflectionHelpers.getField(this, "webViewUnusable")

    private fun MainActivity.notificationManager(): ShadowNotificationManager =
        shadowOf(getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)

    private fun MainActivity.invokeStartLogin() {
        ReflectionHelpers.callInstanceMethod<Unit>(this, "startLogin")
    }

    private fun MainActivity.invokeOnSessionToken(
        token: String,
        origin: String = PINNED_ORIGIN,
    ) {
        ReflectionHelpers.callInstanceMethod<Unit>(
            this,
            "onSessionToken",
            ClassParameter.from(String::class.java, origin),
            ClassParameter.from(String::class.java, token),
        )
    }

    private fun MainActivity.invokeOnPageReady(url: String) {
        ReflectionHelpers.callInstanceMethod<Unit>(
            this,
            "onPageReady",
            ClassParameter.from(String::class.java, url),
        )
    }

    private fun MainActivity.invokeFlushPendingActivation() {
        ReflectionHelpers.callInstanceMethod<Unit>(this, "flushPendingActivation")
    }

    private fun MainActivity.invokeOnNewIntent(intent: Intent) {
        ReflectionHelpers.callInstanceMethod<Unit>(
            this,
            "onNewIntent",
            ClassParameter.from(Intent::class.java, intent),
        )
    }

    private fun MainActivity.startBlockedLogin(): Pair<OidcLoginManager, CountDownLatch> {
        val manager = loginManager()
        val executor: ExecutorService = ReflectionHelpers.getField(manager, "io")
        val workerStarted = CountDownLatch(1)
        val releaseWorker = CountDownLatch(1)
        executor.execute {
            workerStarted.countDown()
            runCatching { releaseWorker.await() }
        }
        assertTrue(workerStarted.await(5, TimeUnit.SECONDS))
        assertTrue(manager.start(this, PINNED_ORIGIN))
        return manager to releaseWorker
    }

    private fun OmnigentWebViewClient.proxyAuthState(): ProxyAuthState =
        ReflectionHelpers.getField(this, "proxyAuthState")

    private fun OmnigentWebViewClient.lastSelfStoppedUrl(): String? =
        ReflectionHelpers.getField(this, "lastSelfStoppedUrl")

    private fun request(
        url: String,
        redirect: Boolean = false,
    ): WebResourceRequest =
        object : WebResourceRequest {
            override fun getUrl(): Uri = Uri.parse(url)

            override fun isForMainFrame(): Boolean = true

            override fun isRedirect(): Boolean = redirect

            override fun hasGesture(): Boolean = false

            override fun getMethod(): String = "GET"

            override fun getRequestHeaders(): Map<String, String> = emptyMap()
        }

    private fun renderProcessGoneDetail() =
        object : RenderProcessGoneDetail() {
            override fun didCrash(): Boolean = true

            override fun rendererPriorityAtExit(): Int = 0
        }

    private fun fileChooserParams() =
        object : WebChromeClient.FileChooserParams() {
            override fun createIntent(): Intent = Intent(Intent.ACTION_OPEN_DOCUMENT)

            override fun getAcceptTypes(): Array<String> = arrayOf("application/pdf")

            override fun getFilenameHint(): String? = null

            override fun getMode(): Int = WebChromeClient.FileChooserParams.MODE_OPEN

            override fun getTitle(): CharSequence? = null

            override fun isCaptureEnabled(): Boolean = false
        }

    private fun browserProbe(): Intent =
        Intent(Intent.ACTION_VIEW, Uri.parse("http:"))
            .addCategory(Intent.CATEGORY_BROWSABLE)

    private fun proxyAuthUrl(origin: String): String =
        "https://idp.example.com/oidc/oauth2/v2.0/authorize?response_type=code" +
            "&redirect_uri=" + Uri.encode("$origin/.auth/callback")

    private fun notificationIntent(
        path: String,
        origin: String?,
    ): Intent =
        Intent(ApplicationProvider.getApplicationContext(), MainActivity::class.java).apply {
            putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, path)
            if (origin != null) {
                putExtra(NativeNotificationManager.EXTRA_NOTIFICATION_ORIGIN, origin)
            }
        }

    private class RejectingCookieManager : RoboCookieManager() {
        override fun setCookie(
            url: String,
            value: String,
            callback: ValueCallback<Boolean>?,
        ) {
            callback?.onReceiveValue(false)
        }
    }

    private class RecordingCookieManager(
        private val accepted: Boolean?,
    ) : RoboCookieManager() {
        var writtenUrl: String? = null
        var writtenCookie: String? = null

        override fun setCookie(
            url: String,
            value: String,
            callback: ValueCallback<Boolean>?,
        ) {
            writtenUrl = url
            writtenCookie = value
            accepted?.let { callback?.onReceiveValue(it) }
        }
    }

    private class RecordingPermissionRequest(
        private val requestOrigin: Uri?,
    ) : PermissionRequest() {
        var denyCalls = 0
        var grantCalls = 0

        override fun getOrigin(): Uri? = requestOrigin

        override fun getResources(): Array<String> = arrayOf(RESOURCE_AUDIO_CAPTURE)

        override fun grant(resources: Array<out String>) {
            grantCalls++
        }

        override fun deny() {
            denyCalls++
        }
    }

    private companion object {
        const val PINNED_ORIGIN = "https://example.com"
        const val NEW_ORIGIN = "https://new.example.com"
        const val NEW_SERVER_URL = "$NEW_ORIGIN/app"
        const val OLD_LOADING_URL = "$PINNED_ORIGIN/old-loading"
        const val PINNED_DOWNLOAD_URL = "$PINNED_ORIGIN/files/report.pdf"
        const val OFF_ORIGIN_DOWNLOAD_URL = "https://files.example.net/report.pdf"
        const val OFF_ORIGIN_DOWNLOAD_COOKIE = "front_door=foreign"
        const val DOWNLOAD_USER_AGENT = "OmnigentTest/1.0"
        const val OFF_ORIGIN_AUTH_URL = "https://idp.example.com/login"
        const val REFUSAL_URL =
            "https://accounts.google.com/v3/signin/rejected?error=disallowed_useragent"
        const val SESSION_TOKEN = "header.payload.signature"

        const val EXTERNAL_BROWSER_PACKAGE = "com.example.browser"
        const val EXTERNAL_BROWSER_ACTIVITY = "com.example.browser.MainActivity"
        const val SECOND_BROWSER_PACKAGE = "org.example.browser"
        const val SECOND_BROWSER_ACTIVITY = "org.example.browser.BrowserActivity"
        const val DEEP_LINK_PACKAGE = "com.example.deep-link"
        const val DEEP_LINK_ACTIVITY = "com.example.deep-link.DeepLinkActivity"
    }

    @Test
    fun `server switcher width bounds follow the published band`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val density = activity.resources.displayMetrics.density
        val recoveryFloor = (48 * density).toInt()
        layout(parent, width = 1000, height = 600)

        val wideBand = ServerSwitcherBand(0.4, 0.6)
        activity.setSwitcherBand(wideBand)
        assertEquals(104, button.maxWidth)
        assertEquals(recoveryFloor, button.minWidth)
        layout(parent, width = 1000, height = 600)
        assertEquals(View.VISIBLE, button.visibility)
        // The label may measure under the cap; placement must respect the reserve.
        assertTrue(button.width <= 104)
        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, wideBand, edgeReserve = 48)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)

        activity.setSwitcherBand(ServerSwitcherBand(0.45, 0.55))
        assertEquals(recoveryFloor, button.maxWidth)
        assertEquals(recoveryFloor, button.minWidth)
        layout(parent, width = 1000, height = 600)
        assertEquals(View.INVISIBLE, button.visibility)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `hidden state clears stale placement until a fresh band is shown`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        layout(parent, width = 1000, height = 600)
        activity.receiveSwitcherBand(ServerSwitcherBand(0.2, 0.8))

        activity.receiveSwitcherHidden(true)

        assertEquals(View.INVISIBLE, button.visibility)
        assertNull(activity.switcherBandOrNull())

        activity.receiveSwitcherBand(ServerSwitcherBand(0.2, 0.8))
        assertEquals(View.INVISIBLE, button.visibility)
        activity.receiveSwitcherHidden(false)
        assertEquals(View.VISIBLE, button.visibility)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `unchanged insets do not request another switcher layout`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val insets =
            WindowInsetsCompat
                .Builder()
                .setInsets(WindowInsetsCompat.Type.systemBars(), Insets.of(0, 24, 0, 0))
                .build()

        ViewCompat.dispatchApplyWindowInsets(activity.webView(), insets)
        layout(parent, width = 1000, height = 600)
        assertFalse(button.isLayoutRequested)

        ViewCompat.dispatchApplyWindowInsets(activity.webView(), insets)

        assertFalse(button.isLayoutRequested)
    }

    private fun layout(
        view: View,
        width: Int,
        height: Int,
    ) {
        // Force the traversal: an unchanged frame otherwise skips onLayout, so
        // children keep their stale positions.
        view.requestLayout()
        view.measure(
            View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY),
        )
        view.layout(0, 0, width, height)
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun MainActivity.switchButton(): TextView =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as TextView

    private fun MainActivity.setSwitcherBand(band: ServerSwitcherBand) {
        MainActivity::class
            .java
            .getDeclaredField("switcherBand")
            .apply { isAccessible = true }
            .set(this, band)
        MainActivity::class
            .java
            .getDeclaredMethod("positionServerSwitcher")
            .apply { isAccessible = true }
            .invoke(this)
    }

    private fun MainActivity.switcherBand(): ServerSwitcherBand =
        MainActivity::class
            .java
            .getDeclaredField("switcherBand")
            .apply { isAccessible = true }
            .get(this) as ServerSwitcherBand

    private fun MainActivity.switcherBandOrNull(): ServerSwitcherBand? =
        MainActivity::class
            .java
            .getDeclaredField("switcherBand")
            .apply { isAccessible = true }
            .get(this) as? ServerSwitcherBand

    private fun View.removeLayoutChangeListeners() {
        val listenerInfo =
            View::class
                .java
                .getDeclaredField("mListenerInfo")
                .apply { isAccessible = true }
                .get(this) ?: return
        val listeners =
            listenerInfo::class
                .java
                .getDeclaredField("mOnLayoutChangeListeners")
                .apply { isAccessible = true }
                .get(listenerInfo) as? Iterable<*> ?: return
        listeners
            .filterIsInstance<View.OnLayoutChangeListener>()
            .toList()
            .forEach(::removeOnLayoutChangeListener)
    }

    private fun MainActivity.receiveSwitcherBand(band: ServerSwitcherBand) {
        val method =
            MainActivity::class
                .java
                .getDeclaredMethod("receiveServerSwitcherBand", ServerSwitcherBand::class.java)
                .apply { isAccessible = true }
        shadowOf(Looper.getMainLooper()).runPaused { method.invoke(this, band) }
    }

    private fun MainActivity.receiveSwitcherHidden(hidden: Boolean) {
        val method =
            MainActivity::class
                .java
                .getDeclaredMethod("receiveServerSwitcherHidden", Boolean::class.javaPrimitiveType)
                .apply { isAccessible = true }
        shadowOf(Looper.getMainLooper()).runPaused { method.invoke(this, hidden) }
    }
}

private data class RecordedDownload(
    val url: String,
    val pinnedOrigin: String,
    val userAgent: String,
    val mimeType: String?,
    val suggestedName: String,
)

private class RecordingPinnedOriginDownloader : PinnedOriginDownloadHandler {
    val downloads = mutableListOf<RecordedDownload>()

    override fun download(
        url: String,
        pinnedOrigin: String,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    ) {
        downloads += RecordedDownload(url, pinnedOrigin, userAgent, mimeType, suggestedName)
    }

    override fun shutdown() = Unit
}

private class RecordingShutdownExecutor(
    private val onShutdown: () -> Unit,
) : AbstractExecutorService() {
    private var shutDown = false

    override fun shutdown() {
        onShutdown()
        shutDown = true
    }

    override fun shutdownNow(): MutableList<Runnable> {
        shutdown()
        return mutableListOf()
    }

    override fun isShutdown(): Boolean = shutDown

    override fun isTerminated(): Boolean = shutDown

    override fun awaitTermination(
        timeout: Long,
        unit: TimeUnit,
    ): Boolean = shutDown

    override fun execute(command: Runnable) {
        if (shutDown) throw RejectedExecutionException()
        command.run()
    }
}

/** Ordered record of the shell calls the Activity makes on its collaborators. */
private object ActivityCallLog {
    val entries = mutableListOf<String>()

    val endProxyAuthCalls: Int get() = entries.count { it == "endProxyAuth" }

    fun clear() = entries.clear()
}

@Implements(OmnigentWebViewClient::class, isInAndroidSdk = false)
class CountingOmnigentWebViewClientShadow {
    @RealObject
    private lateinit var realClient: OmnigentWebViewClient

    @Implementation
    fun endProxyAuth() {
        ActivityCallLog.entries += "endProxyAuth"
        Shadow.directlyOn<Any?>(
            realClient,
            OmnigentWebViewClient::class.java.name,
            "endProxyAuth",
        )
    }
}

@Implements(WebView::class)
class RecordingWebViewShadow : ShadowWebView() {
    @Implementation
    override fun loadUrl(url: String) {
        ActivityCallLog.entries += "loadUrl:$url"
        super.loadUrl(url)
    }

    @Implementation
    fun stopLoading() {
        ActivityCallLog.entries += "stopLoading"
    }
}

@Implements(DownloadManager::class)
class ThrowingDownloadManagerShadow {
    @Implementation
    fun enqueue(request: DownloadManager.Request): Long =
        throw SecurityException("destination rejected")
}
