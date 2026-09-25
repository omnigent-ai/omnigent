package ai.omnigent.android

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class DatabricksCallbackHandoffTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()

    @Test
    fun `marker distinguishes a recent callback handoff and expires`() {
        var now = 1_000L
        val handoff = DatabricksCallbackHandoff(context) { now }
        handoff.clear()

        assertFalse(handoff.isInProgress())
        handoff.markInProgress()
        assertTrue(handoff.isInProgress())

        now += 2 * 60 * 1_000L + 1
        assertFalse(handoff.isInProgress())
    }

    @Test
    fun `clear removes callback handoff`() {
        val handoff = DatabricksCallbackHandoff(context) { 5_000L }
        handoff.markInProgress()

        handoff.clear()

        assertFalse(handoff.isInProgress())
    }
}
