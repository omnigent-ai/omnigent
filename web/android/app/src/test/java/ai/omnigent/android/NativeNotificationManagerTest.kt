package ai.omnigent.android

import android.app.Application
import android.app.NotificationManager
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowNotificationManager

@RunWith(RobolectricTestRunner::class)
class NativeNotificationManagerTest {
    private lateinit var context: Application
    private lateinit var manager: NativeNotificationManager
    private lateinit var shadow: ShadowNotificationManager

    // The reserved badge-summary notification id (NativeNotificationManager's
    // BADGE_NOTIFICATION_ID is private; the contract is "id 1").
    private val badgeId = 1

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        manager = NativeNotificationManager(context)
        shadow =
            shadowOf(
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
            )
    }

    private fun badgeNotification() = shadow.getNotification(badgeId)

    @Test
    fun `badge posts a summary notification with the count and tap intent`() {
        manager.setBadgeCount(2, navigatePath = "/inbox", title = "t", body = "b")

        val posted = badgeNotification()
        assertNotNull(posted)
        assertEquals(2, posted!!.number)
        assertNotNull(posted.contentIntent)
    }

    @Test
    fun `badge count zero cancels the summary notification`() {
        manager.setBadgeCount(3, navigatePath = "/inbox")
        assertNotNull(badgeNotification())

        manager.setBadgeCount(0)

        // The count surface must not linger as a stale, still-tappable
        // "sessions need your attention" once nothing is pending.
        assertNull(badgeNotification())
    }

    @Test
    fun `badge without a path posts with no tap intent`() {
        manager.setBadgeCount(1)
        val posted = badgeNotification()
        assertNotNull(posted)
        assertNull(posted!!.contentIntent)
    }

    @Test
    fun `replayBadge re-posts the badge dropped while notifications were disabled`() {
        // The API 33+ permission dialog is still open: posts drop silently.
        shadow.setNotificationsEnabled(false)
        manager.setBadgeCount(4, navigatePath = "/inbox", title = "t", body = "b")
        assertNull(badgeNotification())

        // Grant lands: MainActivity replays the cached state.
        shadow.setNotificationsEnabled(true)
        manager.replayBadge()

        val posted = badgeNotification()
        assertNotNull(posted)
        assertEquals(4, posted!!.number)
    }

    @Test
    fun `replayBadge of a zero state clears rather than posts`() {
        manager.setBadgeCount(2)
        manager.setBadgeCount(0)
        manager.replayBadge()
        assertNull(badgeNotification())
    }

    @Test
    fun `a new path replaces the badge tap intent extras`() {
        manager.setBadgeCount(1, navigatePath = "/c/conv_a")
        manager.setBadgeCount(2, navigatePath = "/inbox")

        // FLAG_UPDATE_CURRENT on a fixed requestCode must refresh the extras —
        // a stale path would route the tap to the wrong destination.
        val intent = shadowOf(badgeNotification()!!.contentIntent).savedIntent
        assertEquals(
            "/inbox",
            intent.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH),
        )
    }
}
