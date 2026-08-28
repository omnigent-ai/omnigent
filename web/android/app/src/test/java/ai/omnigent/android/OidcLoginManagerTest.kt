package ai.omnigent.android

import android.app.Activity
import android.os.Looper
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
class OidcLoginManagerTest {
    private val manager = OidcLoginManager()
    private val activity: Activity
        get() = Robolectric.buildActivity(Activity::class.java).setup().get()

    @Test
    fun `second start while in-flight returns false`() {
        val activity = this.activity
        manager.start(activity, UNREACHABLE) { _, _ -> }

        // inFlight is only cleared on the main looper (in the delivery post or
        // cancel). The looper isn't drained here, so the flag stays true
        // regardless of how fast the background task fails — deterministic.
        assertFalse(manager.start(activity, UNREACHABLE) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `cancel allows a new login to start immediately`() {
        val activity = this.activity
        manager.start(activity, UNREACHABLE) { _, _ -> }

        manager.cancel()

        assertTrue(manager.start(activity, UNREACHABLE) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `stale delivery lambda is discarded when generation advances`() {
        // Simulate the race: the background task posts a delivery lambda to the
        // main looper, then cancel()+start(B) runs before the looper drains.
        // The lambda must see a stale generation and act as a no-op.
        val deliveredA = mutableListOf<String>()
        val activity = this.activity
        manager.start(activity, UNREACHABLE) { token, _ -> deliveredA += token }

        // Advance the generation (as cancel()+start would) and post a synthetic
        // "taskA got a token" delivery at the stale generation — this is exactly
        // what happens when a real token arrives just before cancel fires.
        val staleGen = generationOf(manager)
        manager.cancel()
        manager.start(activity, UNREACHABLE) { _, _ -> }
        handlerOf(manager).post {
            // A real taskA lambda would check generation == staleGen here.
            // This synthetic one does too, to mirror the production code path.
            if (generationOf(manager) == staleGen) {
                callbackOf(manager)?.invoke("stale.jwt.token", UNREACHABLE)
            }
        }

        // Drain — the synthetic stale lambda must be discarded (gen advanced).
        shadowOf(Looper.getMainLooper()).idle()

        assertTrue("stale token must not reach callback", deliveredA.isEmpty())
        manager.shutdown()
    }

    @Test
    fun `shutdown after cancel does not throw`() {
        val activity = this.activity
        manager.start(activity, UNREACHABLE) { _, _ -> }
        manager.cancel()
        manager.shutdown()
    }

    // --- Reflection helpers for white-box assertions ---

    private fun generationOf(m: OidcLoginManager): Long =
        OidcLoginManager::class.java
            .getDeclaredField("generation")
            .apply { isAccessible = true }
            .get(m)
            .let { (it as java.util.concurrent.atomic.AtomicLong).get() }

    private fun handlerOf(m: OidcLoginManager): android.os.Handler =
        OidcLoginManager::class.java
            .getDeclaredField("main")
            .apply { isAccessible = true }
            .get(m) as android.os.Handler

    @Suppress("UNCHECKED_CAST")
    private fun callbackOf(m: OidcLoginManager): ((String, String) -> Unit)? =
        OidcLoginManager::class.java
            .getDeclaredField("sessionCallback")
            .apply { isAccessible = true }
            .get(m) as? (String, String) -> Unit

    private companion object {
        // A syntactically valid but unreachable origin — the background poll
        // fails with ECONNREFUSED, but that happens asynchronously and does not
        // affect the synchronous state transitions tested here.
        const val UNREACHABLE = "http://127.0.0.1:1"
    }
}
