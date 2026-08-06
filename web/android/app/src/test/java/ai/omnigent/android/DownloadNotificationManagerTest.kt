package ai.omnigent.android

import android.app.Activity
import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowLooper.idleMainLooper
import org.robolectric.shadows.ShadowNotificationManager
import org.robolectric.shadows.ShadowToast
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class DownloadNotificationManagerTest {
    private lateinit var context: Application
    private lateinit var shadow: ShadowNotificationManager

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ShadowNotificationManager.reset()
        ShadowToast.reset()
        context
            .getSharedPreferences(NOTIFICATION_PREFS, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
        context
            .getSharedPreferences(DOWNLOAD_OUTCOME_PREFS, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        DownloadNotificationManager.activityStarted(controller.get())
        DownloadNotificationManager.activityStopped(controller.get())
        controller.destroy()
        shadow =
            shadowOf(
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
            )
    }

    @After
    fun tearDown() {
        ShadowNotificationManager.reset()
    }

    @Test
    fun `download notifications are isolated from session activation and ids`() {
        val sessions = NativeNotificationManager(context, ORIGIN)
        sessions.notify("Session ready", null, "/c/session")
        val notificationIds =
            context.getSharedPreferences(NOTIFICATION_PREFS, Context.MODE_PRIVATE)
        val nextSessionId = notificationIds.getInt(KEY_NEXT_NOTIFICATION_ID, -1)
        val workId = UUID.randomUUID()

        DownloadNotificationManager(context).succeeded("report.pdf", workId)

        val download =
            shadow.getNotification(
                DownloadNotificationManager.notificationTag(workId),
                DownloadNotificationManager.NOTIFICATION_ID,
            )
        assertNotNull(download)
        assertEquals(DownloadNotificationManager.CHANNEL_ID, download!!.channelId)
        assertEquals(
            "Download complete",
            download.extras.getCharSequence(Notification.EXTRA_TITLE),
        )
        assertNull(download.contentIntent)
        assertEquals(nextSessionId, notificationIds.getInt(KEY_NEXT_NOTIFICATION_ID, -1))
        val session = shadow.getNotification(FIRST_SESSION_NOTIFICATION_ID)
        assertNotNull(session)
        assertNotEquals(session!!.channelId, download.channelId)

        sessions.cancelAll()

        assertNull(shadow.getNotification(FIRST_SESSION_NOTIFICATION_ID))
        assertNotNull(
            shadow.getNotification(
                DownloadNotificationManager.notificationTag(workId),
                DownloadNotificationManager.NOTIFICATION_ID,
            ),
        )
    }

    @Test
    fun `disabled notifications fall back to a toast in the foreground`() {
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        val activity = controller.get()
        DownloadNotificationManager.activityStarted(activity)
        shadow.setNotificationsEnabled(false)

        DownloadNotificationManager(context).succeeded("report.pdf", UUID.randomUUID())
        idleMainLooper()

        assertEquals("Saved report.pdf to Downloads", ShadowToast.getTextOfLatestToast())
        DownloadNotificationManager.activityStopped(activity)
        controller.destroy()
    }

    @Test
    fun `blocked download channel falls back to a toast in the foreground`() {
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        val activity = controller.get()
        DownloadNotificationManager.activityStarted(activity)
        val notifications = DownloadNotificationManager(context)
        shadow.notificationChannels
            .single { channel -> channel.id == DownloadNotificationManager.CHANNEL_ID }
            .importance = NotificationManager.IMPORTANCE_NONE
        val workId = UUID.randomUUID()

        notifications.failed("report.pdf", workId)
        idleMainLooper()

        assertNull(
            shadow.getNotification(
                DownloadNotificationManager.notificationTag(workId),
                DownloadNotificationManager.NOTIFICATION_ID,
            ),
        )
        assertEquals("Couldn't download report.pdf", ShadowToast.getTextOfLatestToast())
        DownloadNotificationManager.activityStopped(activity)
        controller.destroy()
    }

    @Test
    fun `background fallback is shown when an activity next reaches foreground`() {
        shadow.setNotificationsEnabled(false)
        DownloadNotificationManager(context).failed("report.pdf", UUID.randomUUID())
        idleMainLooper()
        assertNull(ShadowToast.getLatestToast())
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        val activity = controller.get()

        DownloadNotificationManager.activityStarted(activity)
        idleMainLooper()

        assertEquals("Couldn't download report.pdf", ShadowToast.getTextOfLatestToast())
        DownloadNotificationManager.activityStopped(activity)
        controller.destroy()
    }

    @Test
    fun `queued download acknowledges a repeated foreground tap`() {
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        val activity = controller.get()
        DownloadNotificationManager.activityStarted(activity)
        val downloader =
            PinnedOriginDownloader(
                context,
                PinnedOriginWorkEnqueuer { _, _, _ -> },
            )

        repeat(2) {
            downloader.download(
                "$ORIGIN/report.pdf",
                ORIGIN,
                "OmnigentTest/1.0",
                "application/pdf",
                "report.pdf",
            )
        }
        idleMainLooper()

        assertEquals("Download queued: report.pdf", ShadowToast.getTextOfLatestToast())
        assertEquals(2, ShadowToast.shownToastCount())
        DownloadNotificationManager.activityStopped(activity)
        controller.destroy()
    }

    private companion object {
        const val ORIGIN = "https://example.com"
        const val FIRST_SESSION_NOTIFICATION_ID = 2
        const val NOTIFICATION_PREFS = "ai.omnigent.android.notifications"
        const val DOWNLOAD_OUTCOME_PREFS = "ai.omnigent.android.download_outcomes"
        const val KEY_NEXT_NOTIFICATION_ID = "next_notification_id"
    }
}
