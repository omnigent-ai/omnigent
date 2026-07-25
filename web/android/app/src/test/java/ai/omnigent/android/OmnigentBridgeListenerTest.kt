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

/**
 * Bridge JSON parsing, asserted end to end through the real
 * [NativeNotificationManager] into Robolectric's shadow notification
 * service — the same wiring [MainActivity] installs.
 */
@RunWith(RobolectricTestRunner::class)
class OmnigentBridgeListenerTest {
    private lateinit var context: Application
    private lateinit var listener: OmnigentBridgeListener
    private lateinit var shadow: ShadowNotificationManager

    private val badgeId = 1

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        listener =
            OmnigentBridgeListener(
                notifications = NativeNotificationManager(context),
                blobSaver = BlobSaver(context),
            )
        shadow =
            shadowOf(
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
            )
    }

    @Test
    fun `setBadgeCount message posts the badge with parsed fields`() {
        listener.handle(
            """{"method":"setBadgeCount","count":3,"navigatePath":"/inbox","title":"T","body":"B"}""",
        )

        val posted = shadow.getNotification(badgeId)
        assertNotNull(posted)
        assertEquals(3, posted!!.number)
        assertEquals(
            "/inbox",
            shadowOf(posted.contentIntent).savedIntent.getStringExtra(
                NativeNotificationManager.EXTRA_NAVIGATE_PATH,
            ),
        )
    }

    @Test
    fun `setBadgeCount zero clears the badge`() {
        listener.handle("""{"method":"setBadgeCount","count":2,"navigatePath":"/inbox"}""")
        listener.handle("""{"method":"setBadgeCount","count":0}""")
        assertNull(shadow.getNotification(badgeId))
    }

    @Test
    fun `legacy setBadgeCount without options still posts`() {
        // Older web builds send only the count; fields default to absent.
        listener.handle("""{"method":"setBadgeCount","count":1}""")
        val posted = shadow.getNotification(badgeId)
        assertNotNull(posted)
        assertNull(posted!!.contentIntent)
    }

    @Test
    fun `notify message posts a per-session toast with tap routing`() {
        listener.handle(
            """{"method":"notify","params":{"title":"done","body":"b","navigatePath":"/c/x"}}""",
        )

        // Toasts allocate ids above the reserved badge id.
        assertEquals(1, shadow.allNotifications.size)
        assertNull(shadow.getNotification(badgeId))
    }

    @Test
    fun `notify without a title is dropped`() {
        listener.handle("""{"method":"notify","params":{"body":"b"}}""")
        assertEquals(0, shadow.allNotifications.size)
    }

    @Test
    fun `malformed and unknown messages are dropped without crashing`() {
        listener.handle("not json at all")
        listener.handle("""{"method":"unknownThing","count":5}""")
        listener.handle("""{"count":5}""")
        assertEquals(0, shadow.allNotifications.size)
    }
}
