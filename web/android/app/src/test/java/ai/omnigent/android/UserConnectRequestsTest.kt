package ai.omnigent.android

import android.widget.Button
import android.widget.EditText
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class UserConnectRequestsTest {
    @Test
    fun `connect screen starts the shell with a request that counts once`() {
        val activity = Robolectric.buildActivity(ConnectActivity::class.java).setup().get()
        activity.findViewById<EditText>(R.id.server_url).setText("https://example.com")
        activity.findViewById<Button>(R.id.connect).performClick()

        val started = shadowOf(activity).nextStartedActivity
        val token = started.getStringExtra(MainActivity.EXTRA_CONNECT_REQUEST)

        assertEquals(MainActivity::class.java.name, started.component!!.className)
        assertTrue(UserConnectRequests.consume(token))
        // A restored activity is relaunched with the same intent: not a fresh tap.
        assertFalse(UserConnectRequests.consume(token))
    }

    @Test
    fun `a token this process did not issue is not a request`() {
        val issued = UserConnectRequests.issue()

        assertFalse(UserConnectRequests.consume(null))
        assertFalse(UserConnectRequests.consume("from-before-process-death"))
        assertTrue(UserConnectRequests.consume(issued))
    }

    @Test
    fun `only the latest request counts`() {
        val superseded = UserConnectRequests.issue()
        val latest = UserConnectRequests.issue()

        assertFalse(UserConnectRequests.consume(superseded))
        assertTrue(UserConnectRequests.consume(latest))
    }
}
