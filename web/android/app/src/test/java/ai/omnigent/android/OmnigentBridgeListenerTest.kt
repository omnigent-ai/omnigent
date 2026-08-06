package ai.omnigent.android

import android.app.Application
import android.app.NotificationManager
import android.content.Context
import android.net.Uri
import androidx.appcompat.app.AppCompatDelegate
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
    private lateinit var notifications: NativeNotificationManager
    private lateinit var shadow: ShadowNotificationManager
    private val receivedBands = mutableListOf<ServerSwitcherBand>()
    private val receivedSwitcherVisibility = mutableListOf<Boolean>()
    private var pinnedOrigin: String? = ORIGIN

    private val badgeId = 1

    @Before
    fun setUp() {
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM)
        context = ApplicationProvider.getApplicationContext()
        pinnedOrigin = ORIGIN
        notifications = NativeNotificationManager(context, ORIGIN)
        listener =
            OmnigentBridgeListener(
                notifications = notifications,
                blobSaver = BlobSaver(context),
                onServerSwitcherBand = { receivedBands += it },
                onServerSwitcherHidden = { receivedSwitcherVisibility += it },
                pinnedOrigin = { pinnedOrigin },
            )
        shadow =
            shadowOf(
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
            )
    }

    @Test
    fun `setColorScheme light sets night mode no`() {
        listener.handle("""{"method":"setColorScheme","scheme":"light"}""")
        assertEquals(AppCompatDelegate.MODE_NIGHT_NO, AppCompatDelegate.getDefaultNightMode())
    }

    @Test
    fun `setColorScheme dark sets night mode yes`() {
        listener.handle("""{"method":"setColorScheme","scheme":"dark"}""")
        assertEquals(AppCompatDelegate.MODE_NIGHT_YES, AppCompatDelegate.getDefaultNightMode())
    }

    @Test
    fun `setColorScheme system follows system`() {
        listener.handle("""{"method":"setColorScheme","scheme":"system"}""")
        assertEquals(
            AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM,
            AppCompatDelegate.getDefaultNightMode(),
        )
    }

    @Test
    fun `setColorScheme rejects missing and unsupported schemes`() {
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO)

        listener.handle("""{"method":"setColorScheme"}""")
        listener.handle("""{"method":"setColorScheme","scheme":"auto"}""")
        listener.handle("""{"method":"setColorScheme","scheme":123}""")

        assertEquals(AppCompatDelegate.MODE_NIGHT_NO, AppCompatDelegate.getDefaultNightMode())
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
    fun `queued message from the previous origin is dropped after a server switch`() {
        pinnedOrigin = NEW_ORIGIN
        notifications.setOrigin(NEW_ORIGIN)

        listener.handle(
            """{"method":"notify","params":{"title":"stale","navigatePath":"/c/a"}}""",
            Uri.parse(ORIGIN),
            true,
        )

        assertEquals(0, shadow.allNotifications.size)
    }

    @Test
    fun `opaque message is dropped when no origin is pinned`() {
        pinnedOrigin = null

        listener.handle(
            """{"method":"notify","params":{"title":"opaque"}}""",
            Uri.parse("about:blank"),
            true,
        )

        assertEquals(0, shadow.allNotifications.size)
    }

    @Test
    fun `malformed and unknown messages are dropped without crashing`() {
        listener.handle("not json at all")
        listener.handle("""{"method":"unknownThing","count":5}""")
        listener.handle("""{"count":5}""")
        assertEquals(0, shadow.allNotifications.size)
    }

    @Test
    fun `setServerSwitcherBand dispatches valid normalized fractions`() {
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":0.25,"rightFraction":0.8}""",
        )

        assertEquals(listOf(ServerSwitcherBand(0.25, 0.8)), receivedBands)
    }

    @Test
    fun `setServerSwitcherBand drops missing and wrong-type fields`() {
        listener.handle("""{"method":"setServerSwitcherBand","leftFraction":0.25}""")
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":"0.25","rightFraction":0.8}""",
        )
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":0.25,"rightFraction":null}""",
        )

        assertEquals(emptyList<ServerSwitcherBand>(), receivedBands)
    }

    @Test
    fun `setServerSwitcherBand drops non-finite out-of-range and reversed fractions`() {
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":0.2,"rightFraction":1e400}""",
        )
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":-0.1,"rightFraction":0.8}""",
        )
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":0.2,"rightFraction":1.1}""",
        )
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":0.8,"rightFraction":0.2}""",
        )
        listener.handle(
            """{"method":"setServerSwitcherBand","leftFraction":0.5,"rightFraction":0.5}""",
        )

        assertEquals(emptyList<ServerSwitcherBand>(), receivedBands)
    }

    @Test
    fun `setServerSwitcherHidden dispatches booleans`() {
        listener.handle("""{"method":"setServerSwitcherHidden","hidden":true}""")
        listener.handle("""{"method":"setServerSwitcherHidden","hidden":false}""")

        assertEquals(listOf(true, false), receivedSwitcherVisibility)
    }

    @Test
    fun `setServerSwitcherHidden rejects missing and non-boolean values`() {
        listener.handle("""{"method":"setServerSwitcherHidden"}""")
        listener.handle("""{"method":"setServerSwitcherHidden","hidden":"true"}""")
        listener.handle("""{"method":"setServerSwitcherHidden","hidden":1}""")
        listener.handle("""{"method":"setServerSwitcherHidden","hidden":null}""")

        assertEquals(emptyList<Boolean>(), receivedSwitcherVisibility)
    }

    private companion object {
        const val ORIGIN = "https://example.com"
        const val NEW_ORIGIN = "https://new.example.com"
    }
}
