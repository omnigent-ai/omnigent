package ai.omnigent.android

import android.app.Application
import android.app.NotificationChannel
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
    private lateinit var notificationManager: NotificationManager

    // The reserved badge-summary notification id (NativeNotificationManager's
    // BADGE_NOTIFICATION_ID is private; the contract is "id 1").
    private val badgeId = 1

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        manager = NativeNotificationManager(context)
        notificationManager =
            context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        shadow = shadowOf(notificationManager)
    }

    private fun badgeNotification() = shadow.getNotification(badgeId)

    @Test
    fun `constructing the manager creates the sessions channel`() {
        // The background poll worker constructs a fresh manager in a process
        // where no Activity ran; the channel must exist so an O+ post isn't
        // silently dropped. `manager` is built in setUp — assert the channel is
        // present with the expected id and importance.
        val channel: NotificationChannel? =
            notificationManager.getNotificationChannel(
                "omnigent.sessions",
            )
        assertNotNull(channel)
        assertEquals(NotificationManager.IMPORTANCE_HIGH, channel!!.importance)
    }

    @Test
    fun `ensureChannel is idempotent and keeps the channel present`() {
        // Cheap to call repeatedly — the worker path relies on this being safe.
        manager.ensureChannel()
        manager.ensureChannel()
        assertNotNull(notificationManager.getNotificationChannel("omnigent.sessions"))
    }

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
    fun `tagged posts with a colliding numeric id coexist instead of replacing`() {
        // "Aa" and "BB" have equal String.hashCode, so their derived numeric
        // notification ids collide. The session-id tag must keep them distinct —
        // an untagged post would let the second silently replace the first.
        val idA = notificationIdFor("Aa")
        val idB = notificationIdFor("BB")
        assertEquals(idA, idB)

        manager.notify(
            title = "A",
            body = "a",
            navigatePath = "/c/Aa",
            notificationId = idA,
            tag = "Aa",
        )
        manager.notify(
            title = "B",
            body = "b",
            navigatePath = "/c/BB",
            notificationId = idB,
            tag = "BB",
        )

        assertEquals(2, shadow.size())
        assertNotNull(shadow.getNotification("Aa", idA))
        assertNotNull(shadow.getNotification("BB", idB))
    }

    @Test
    fun `re-posting the same tag and id updates in place`() {
        val id = notificationIdFor("conv_a")
        manager.notify(
            title = "first",
            body = "b",
            navigatePath = "/c/conv_a",
            notificationId = id,
            tag = "conv_a",
        )
        manager.notify(
            title = "second",
            body = "b",
            navigatePath = "/c/conv_a",
            notificationId = id,
            tag = "conv_a",
        )
        assertEquals(1, shadow.size())
    }

    @Test
    fun `cancelAll cancels tagged session notifications too`() {
        manager.notify(
            title = "A",
            body = "a",
            navigatePath = "/c/Aa",
            notificationId = notificationIdFor("Aa"),
            tag = "Aa",
        )
        manager.setBadgeCount(1)
        assertEquals(2, shadow.size())

        manager.cancelAll()

        assertEquals(0, shadow.size())
    }

    @Test
    fun `colliding-id tap intents route to their own session`() {
        // Same numeric id ⇒ same PendingIntent requestCode. The intents must
        // still be distinct (unique data Uri), or FLAG_UPDATE_CURRENT would
        // overwrite the first session's extras and both taps would deep-link
        // to the second session.
        val idA = notificationIdFor("Aa")
        manager.notify(
            title = "A",
            body = "a",
            navigatePath = "/c/Aa",
            notificationId = idA,
            tag = "Aa",
        )
        manager.notify(
            title = "B",
            body = "b",
            navigatePath = "/c/BB",
            notificationId = idA,
            tag = "BB",
        )

        val intentA = shadowOf(shadow.getNotification("Aa", idA)!!.contentIntent).savedIntent
        val intentB = shadowOf(shadow.getNotification("BB", idA)!!.contentIntent).savedIntent
        assertEquals("/c/Aa", intentA.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH))
        assertEquals("/c/BB", intentB.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH))
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
