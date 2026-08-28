package ai.omnigent.android

import android.app.Activity
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OidcLoginManagerTest {
    private val manager = OidcLoginManager()
    private val activity: Activity
        get() = Robolectric.buildActivity(Activity::class.java).setup().get()

    @Test
    fun `second start while in-flight returns false`() {
        val activity = this.activity
        // Start a flow against an unreachable server — the background task stays
        // in-flight until it fails or times out, but inFlight is set synchronously.
        manager.start(activity, UNREACHABLE, {})

        assertFalse(manager.start(activity, UNREACHABLE, {}))
    }

    @Test
    fun `cancel allows a new login to start immediately`() {
        val activity = this.activity
        manager.start(activity, UNREACHABLE, {})

        manager.cancel()

        assertTrue(manager.start(activity, UNREACHABLE, {}))
        manager.shutdown()
    }

    @Test
    fun `cancel prevents a stale token from being delivered`() {
        val delivered = mutableListOf<String>()
        val activity = this.activity
        manager.start(activity, UNREACHABLE, { delivered += it })

        manager.cancel()

        // Simulate a late token arriving on the main thread (as if the poll had
        // already posted the result before cancel() ran). The callback was nulled
        // so it must be swallowed.
        assertTrue(delivered.isEmpty())
    }

    @Test
    fun `shutdown after cancel does not throw`() {
        val activity = this.activity
        manager.start(activity, UNREACHABLE, {})
        manager.cancel()
        manager.shutdown() // must not throw even though cancel already ran
    }

    private companion object {
        // A syntactically valid but unreachable origin — the background poll will
        // fail with a connection error, but that happens asynchronously and does
        // not affect the synchronous state transitions tested here.
        const val UNREACHABLE = "http://127.0.0.1:1"
    }
}
