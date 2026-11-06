package ai.omnigent.android

import android.content.Context
import android.graphics.Color
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
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
import org.robolectric.shadows.ShadowAlertDialog

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class UserConnectRequestsTest {
    @Test
    @Config(qualifiers = "night")
    fun `first launch in dark mode shows a light wordmark on Connect`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        assertFalse(ServerStore(context).hasServer())
        val mainActivity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val connectIntent = shadowOf(mainActivity).nextStartedActivity
        assertEquals(ConnectActivity::class.java.name, connectIntent.component!!.className)

        val connectActivity =
            Robolectric.buildActivity(ConnectActivity::class.java, connectIntent).setup().get()
        assertEquals(Color.WHITE, connectActivity.getColor(R.color.omnigents_wordmark))
        val content = connectActivity.findViewById<ViewGroup>(android.R.id.content)
        val scrollView = content.getChildAt(0) as ViewGroup
        val column = scrollView.getChildAt(0) as ViewGroup
        val logo = column.getChildAt(0) as ImageView
        assertNotNull(logo.drawable)
        connectActivity.resources.getXml(R.drawable.omnigents_logo).use { parser ->
            parser.next()
            parser.nextTag()
            parser.nextTag()
            assertEquals("path", parser.name)
            assertEquals(
                R.color.omnigents_wordmark,
                parser.getAttributeResourceValue(
                    "http://schemas.android.com/apk/res/android",
                    "fillColor",
                    0,
                ),
            )
        }
    }

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
    fun `about shows the installed build version without connecting`() {
        val activity = Robolectric.buildActivity(ConnectActivity::class.java).setup().get()

        activity.findViewById<Button>(R.id.about).performClick()

        val dialog = ShadowAlertDialog.getLatestAlertDialog()
        assertTrue(dialog.isShowing)
        assertEquals(
            activity.getString(
                R.string.about_version,
                BuildConfig.VERSION_NAME,
                BuildConfig.VERSION_CODE,
            ),
            dialog.findViewById<android.widget.TextView>(android.R.id.message)?.text,
        )
        assertNull(shadowOf(activity).nextStartedActivity)
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
