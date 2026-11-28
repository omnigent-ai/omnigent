package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import android.os.Looper
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import java.util.concurrent.Executor

@RunWith(RobolectricTestRunner::class)
class OAuthCallbackActivityTest {
    private val queued = mutableListOf<Runnable>()
    private lateinit var original: Executor

    @Before
    fun holdCompletions() {
        original = OAuthCallbackCompletions.executor
        OAuthCallbackCompletions.executor = Executor { queued += it }
    }

    @After
    fun restoreCompletions() {
        OAuthCallbackCompletions.executor = original
    }

    @Test
    fun `a callback screen recreated mid exchange completes and returns once`() {
        val callback =
            Intent(
                Intent.ACTION_VIEW,
                Uri.parse("https://login.databricks.com/mobile-redirect?code=c&state=s"),
            )
        val controller =
            Robolectric.buildActivity(OAuthCallbackActivity::class.java, callback).setup()

        // Rotation while the exchange is still running.
        controller.recreate()
        assertEquals(1, queued.size)
        queued.single().run()
        shadowOf(Looper.getMainLooper()).idle()

        val app = shadowOf(ApplicationProvider.getApplicationContext<android.app.Application>())
        val returned = app.nextStartedActivity
        assertEquals(MainActivity::class.java.name, returned.component?.className)
        assertTrue(returned.getBooleanExtra(OAuthCallbackActivity.EXTRA_OAUTH_CALLBACK, false))
        assertNull(app.nextStartedActivity)
        assertTrue(controller.get().isFinishing)
    }
}
