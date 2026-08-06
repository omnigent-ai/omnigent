package ai.omnigent.android

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.ForegroundInfo
import java.lang.ref.WeakReference
import java.util.UUID

/** Background-safe completion notifications for durable downloads. */
internal class DownloadNotificationManager(
    private val context: Context,
) {
    private val manager = NotificationManagerCompat.from(context)

    init {
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.download_notification_channel_name),
                NotificationManager.IMPORTANCE_DEFAULT,
            ),
        )
    }

    fun succeeded(
        name: String,
        workId: UUID,
    ) {
        val body =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                context.getString(R.string.download_complete_body_downloads, name)
            } else {
                context.getString(R.string.download_complete_body_app_storage, name)
            }
        post(
            workId,
            context.getString(R.string.download_complete_title),
            body,
        )
    }

    fun authenticationRequired(
        name: String,
        workId: UUID,
    ) {
        post(
            workId,
            context.getString(R.string.download_failed_title),
            context.getString(R.string.download_sign_in_again_body, name),
        )
    }

    fun failed(
        name: String,
        workId: UUID,
    ) {
        post(
            workId,
            context.getString(R.string.download_failed_title),
            context.getString(R.string.download_failed_body, name),
        )
    }

    fun queued(name: String) {
        DownloadOutcomeFallback.showIfForeground(
            context,
            context.getString(R.string.download_queued_body, name),
        )
    }

    fun foregroundInfo(
        name: String,
        workId: UUID,
    ): ForegroundInfo {
        val notification =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(context.getString(R.string.download_in_progress_title, name))
                .setContentText(context.getString(R.string.download_in_progress_body))
                .setCategory(Notification.CATEGORY_PROGRESS)
                .setOngoing(true)
                .setProgress(0, 0, true)
                .build()
        val notificationId = foregroundNotificationId(workId)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ForegroundInfo(
                notificationId,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            ForegroundInfo(notificationId, notification)
        }
    }

    private fun post(
        workId: UUID,
        title: String,
        body: String,
    ) {
        val channelBlocked =
            manager.getNotificationChannelCompat(CHANNEL_ID)?.importance ==
                NotificationManager.IMPORTANCE_NONE
        if (!manager.areNotificationsEnabled() || channelBlocked) {
            DownloadOutcomeFallback.deliverOrRemember(context, workId, body)
            return
        }
        val notification =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body)
                .setCategory(Notification.CATEGORY_STATUS)
                .setAutoCancel(true)
                .build()
        try {
            manager.notify(notificationTag(workId), NOTIFICATION_ID, notification)
        } catch (_: SecurityException) {
            DownloadOutcomeFallback.deliverOrRemember(context, workId, body)
        }
    }

    companion object {
        internal const val CHANNEL_ID = "omnigent.downloads"
        internal const val NOTIFICATION_ID = 0
        private const val NOTIFICATION_TAG_PREFIX = "ai.omnigent.android.download."

        internal fun notificationTag(workId: UUID): String = NOTIFICATION_TAG_PREFIX + workId

        internal fun activityStarted(activity: Activity) {
            DownloadOutcomeFallback.activityStarted(activity)
        }

        internal fun activityStopped(activity: Activity) {
            DownloadOutcomeFallback.activityStopped(activity)
        }

        private fun foregroundNotificationId(workId: UUID): Int =
            Int.MIN_VALUE + Math.floorMod(workId.hashCode(), Int.MAX_VALUE - 1)
    }
}

private object DownloadOutcomeFallback {
    private const val PREFS = "ai.omnigent.android.download_outcomes"
    private const val KEY_PREFIX = "outcome."
    private val lock = Any()
    private val main = Handler(Looper.getMainLooper())
    private var foregroundActivity = WeakReference<Activity>(null)

    fun activityStarted(activity: Activity) {
        if (activity.isFinishing || activity.isDestroyed) return
        // Registration and drain stay atomic with deliverOrRemember's
        // check-then-commit, or a message committed in between is stranded.
        val pending =
            synchronized(lock) {
                foregroundActivity = WeakReference(activity)
                val preferences = activity.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                val messages =
                    preferences.all
                        .filterKeys { key -> key.startsWith(KEY_PREFIX) }
                        .values
                        .filterIsInstance<String>()
                if (messages.isNotEmpty()) {
                    preferences
                        .edit()
                        .apply {
                            preferences.all.keys
                                .filter { key -> key.startsWith(KEY_PREFIX) }
                                .forEach { key -> remove(key) }
                        }.commit()
                }
                messages
            }
        pending.forEach { message -> showIfForeground(activity, message) }
    }

    fun activityStopped(activity: Activity) {
        synchronized(lock) {
            if (foregroundActivity.get() === activity) foregroundActivity.clear()
        }
    }

    fun deliverOrRemember(
        context: Context,
        workId: UUID,
        message: String,
    ) {
        synchronized(lock) {
            if (showIfForeground(context, message)) return
            context
                .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_PREFIX + workId, message)
                .commit()
        }
    }

    fun showIfForeground(
        context: Context,
        message: String,
    ): Boolean {
        val activity = synchronized(lock) { foregroundActivity.get() }
        if (activity == null || activity.isFinishing || activity.isDestroyed) return false
        main.post { Toast.makeText(context, message, Toast.LENGTH_SHORT).show() }
        return true
    }
}
