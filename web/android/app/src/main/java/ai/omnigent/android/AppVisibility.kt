package ai.omnigent.android

import java.util.Collections
import java.util.IdentityHashMap

/**
 * Process-wide "is the app on screen" signal for [SessionPollWorker].
 *
 * While [MainActivity] is started, the SPA's own `useIdleNotifications` hook is
 * live and posts its in-app notification for the very same running → idle /
 * elicitation edges the background poller detects — and the two use different
 * notification-id spaces, so nothing collapses. The worker therefore skips
 * POSTING while the app is visible (holding the un-posted edges; see
 * holdSuppressedTransitions).
 *
 * Deliberately an in-memory tracker rather than a ProcessLifecycleOwner
 * dependency: WorkManager runs the worker in this same process, so the state
 * is reachable and correct — a cold background-only process never starts an
 * Activity and reads as not visible.
 *
 * Tracks the IDENTITIES of started activities (not a bare count) so an
 * unmatched or duplicated lifecycle callback cannot skew the edges: with a
 * counter, an unmatched stop at count 1 would fake a 1 → 0 (pausing WebView
 * timers under a still-visible activity), and an unmatched start would absorb
 * the eventual genuine 1 → 0. With a set, a stop only counts if that same
 * activity is currently started, and a repeated start of the same activity is
 * a no-op.
 */
object AppVisibility {
    private val lock = Any()
    private val startedActivities =
        Collections.newSetFromMap(IdentityHashMap<Any, Boolean>())

    /** True while at least one tracked Activity is between onStart and onStop. */
    val isAppVisible: Boolean get() = synchronized(lock) { startedActivities.isNotEmpty() }

    /**
     * Returns true only on the none-started → some-started transition — "the
     * app just came on screen". Callers gate process-global work
     * (WebView.resumeTimers) on this rather than acting per-activity: during
     * in-process recreation the NEW instance's onStart can run BEFORE the old
     * instance's onStop, and per-activity calls would let the dying instance
     * later act on state the live one owns.
     */
    fun onActivityStarted(activity: Any): Boolean =
        synchronized(lock) {
            val wasHidden = startedActivities.isEmpty()
            startedActivities.add(activity) && wasHidden
        }

    /**
     * Returns true only on the some-started → none-started transition — "the
     * app just left the screen". Only then may process-global work
     * (WebView.pauseTimers) run: with the overlap ordering above, the old
     * instance's onStop leaves the new instance in the set and must NOT pause
     * the timers of the already-foregrounded new instance (that would freeze
     * all WebView JS until the user backgrounds and returns). The
     * recreate()/locale ordering (old stop, then new start) yields a transient
     * pause immediately followed by a resume — both idempotent, so that's
     * harmless. A stop for an activity that was never started (or already
     * stopped) removes nothing and signals no edge.
     */
    fun onActivityStopped(activity: Any): Boolean =
        synchronized(lock) {
            startedActivities.remove(activity) &&
                startedActivities.isEmpty()
        }
}
