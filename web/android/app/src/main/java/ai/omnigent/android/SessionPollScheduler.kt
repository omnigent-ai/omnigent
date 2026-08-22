package ai.omnigent.android

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/**
 * Enqueues the background session poll ([SessionPollWorker]) as unique periodic
 * work. Scheduled at app start and after a successful login; WorkManager
 * persists the request across process death and reboots, so the poll survives
 * without a boot receiver.
 */
object SessionPollScheduler {
    private const val UNIQUE_WORK_NAME = "ai.omnigent.android.session-poll"

    /**
     * Ensure the periodic poll is enqueued. [ExistingPeriodicWorkPolicy.KEEP]
     * makes this idempotent — calling it every launch won't reset the interval
     * or drop an in-flight run; the existing schedule is kept.
     */
    fun ensureScheduled(context: Context) {
        val constraints =
            Constraints
                .Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
        // 15 min is WorkManager's PeriodicWork floor; a shorter interval is
        // silently clamped to it. This is the interim latency ceiling.
        val request =
            PeriodicWorkRequestBuilder<SessionPollWorker>(15, TimeUnit.MINUTES)
                .setConstraints(constraints)
                .build()
        try {
            WorkManager
                .getInstance(context)
                .enqueueUniquePeriodicWork(
                    UNIQUE_WORK_NAME,
                    ExistingPeriodicWorkPolicy.KEEP,
                    request,
                )
        } catch (e: IllegalStateException) {
            // WorkManager has no initializer in this process (JVM unit tests).
            // The poll is a best-effort convenience; never fail activity start.
            android.util.Log.w("SessionPollScheduler", "WorkManager unavailable; poll not scheduled", e)
        }
    }
}
