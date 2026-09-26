package ai.omnigent.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Plain-JVM tests for the started-activity identity tracker gating
 * background-poll notification posting AND the process-global WebView
 * pauseTimers()/resumeTimers() calls. Activities are stand-in objects — the
 * tracker only uses identity. Each test balances its starts with stops so the
 * process-wide singleton doesn't leak state across tests.
 */
class AppVisibilityTest {
    @Test
    fun `app is visible between start and stop only, with edge signals`() {
        val activity = Any()
        assertFalse(AppVisibility.isAppVisible)
        // Came on screen — the ONLY start allowed to resumeTimers().
        assertTrue(AppVisibility.onActivityStarted(activity))
        assertTrue(AppVisibility.isAppVisible)
        // Left the screen — the ONLY stop allowed to pauseTimers().
        assertTrue(AppVisibility.onActivityStopped(activity))
        assertFalse(AppVisibility.isAppVisible)
    }

    @Test
    fun `overlap recreation never signals a pause while the app stays on screen`() {
        // In-process recreation ordering: the NEW instance's onStart runs
        // BEFORE the old instance's onStop. The old instance's stop leaves the
        // new one started and must NOT report an edge — an unconditional
        // per-activity pauseTimers() there would freeze the foregrounded
        // instance's JS.
        val old = Any()
        val new = Any()
        assertTrue(AppVisibility.onActivityStarted(old))
        assertFalse(AppVisibility.onActivityStarted(new)) // no resume needed
        assertFalse(AppVisibility.onActivityStopped(old)) // MUST not pause
        assertTrue(AppVisibility.isAppVisible)
        assertTrue(AppVisibility.onActivityStopped(new)) // pause
        assertFalse(AppVisibility.isAppVisible)
    }

    @Test
    fun `distinct equal activity objects are tracked by reference identity`() {
        val old = EqualActivity(1)
        val replacement = EqualActivity(1)
        assertTrue(old == replacement)

        assertTrue(AppVisibility.onActivityStarted(old))
        assertFalse(AppVisibility.onActivityStarted(replacement))
        assertFalse(AppVisibility.onActivityStopped(old))
        assertTrue(AppVisibility.isAppVisible)
        assertTrue(AppVisibility.onActivityStopped(replacement))
    }

    @Test
    fun `recreate ordering yields a transient pause immediately undone by resume`() {
        // recreate()/locale ordering: old onStop THEN new onStart. Transient
        // pause followed by resume — both idempotent, harmless.
        val old = Any()
        val new = Any()
        assertTrue(AppVisibility.onActivityStarted(old))
        assertTrue(AppVisibility.onActivityStopped(old)) // transient pause
        assertTrue(AppVisibility.onActivityStarted(new)) // immediate resume
        assertTrue(AppVisibility.isAppVisible)
        assertTrue(AppVisibility.onActivityStopped(new))
    }

    @Test
    fun `an unmatched stop under a visible activity cannot fake a pause`() {
        // The identity set is what makes this safe: with a bare counter, this
        // stray stop would decrement 1 -> 0 and pause WebView timers under a
        // still-visible activity.
        val visible = Any()
        val neverStarted = Any()
        assertTrue(AppVisibility.onActivityStarted(visible))
        assertFalse(AppVisibility.onActivityStopped(neverStarted))
        assertTrue(AppVisibility.isAppVisible)
        assertTrue(AppVisibility.onActivityStopped(visible))
    }

    @Test
    fun `a duplicated start cannot absorb the genuine hide edge`() {
        // With a bare counter, a doubled onStart callback would leave the
        // count at 1 after the genuine stop — visible forever, and the real
        // 1 -> 0 edge (the pause) never fires.
        val activity = Any()
        assertTrue(AppVisibility.onActivityStarted(activity))
        assertFalse(AppVisibility.onActivityStarted(activity)) // duplicate: no-op
        assertTrue(AppVisibility.onActivityStopped(activity)) // genuine hide still edges
        assertFalse(AppVisibility.isAppVisible)
    }

    @Test
    fun `a doubled stop signals the edge exactly once`() {
        val activity = Any()
        assertTrue(AppVisibility.onActivityStarted(activity))
        assertTrue(AppVisibility.onActivityStopped(activity))
        assertFalse(AppVisibility.onActivityStopped(activity)) // already gone: no edge
        assertFalse(AppVisibility.isAppVisible)
    }

    private data class EqualActivity(
        val value: Int,
    )
}
