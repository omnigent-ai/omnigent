package ai.omnigent.android

import android.app.Application
import android.util.Log
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.shadows.ShadowLog

@RunWith(RobolectricTestRunner::class)
class BlobSaverTest {
    @Test
    fun `save after shutdown is dropped with a warning`() {
        val context: Application = ApplicationProvider.getApplicationContext()
        val saver = BlobSaver(context)
        ShadowLog.clear()
        saver.shutdown()

        saver.save("aGVsbG8=", "text/plain", "hello.txt")

        val warning = ShadowLog.getLogsForTag("BlobSaver").single()
        assertEquals(Log.WARN, warning.type)
        assertEquals("Dropping blob save because the worker is shut down", warning.msg)
    }
}
