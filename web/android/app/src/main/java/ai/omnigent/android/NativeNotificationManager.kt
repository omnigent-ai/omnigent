package ai.omnigent.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import java.util.concurrent.atomic.AtomicInteger

/**
 * Local (foreground) notifications + best-effort badge, mirroring the iOS
 * `NativeNotificationManager`. Tap routing forwards the notification's
 * `navigatePath` back into the SPA: the tap launches [MainActivity] with the
 * path as an intent extra, which the activity replays via
 * `window.__omnigentNativeEmitNotificationActivated`.
 *
 * Posting tolerates a missing `POST_NOTIFICATIONS` grant (requested by
 * [MainActivity] on API 33+): [post] drops silently if disabled or revoked, so
 * the web layer keeps working without OS toasts.
 */
class NativeNotificationManager(
    private val context: Context,
) {
    private val manager = NotificationManagerCompat.from(context)

    // Ids at/above BADGE_NOTIFICATION_ID + 1 so per-session toasts never collide
    // with the reserved badge-summary notification.
    private val nextId = AtomicInteger(BADGE_NOTIFICATION_ID + 1)

    init {
        val channel =
            NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_HIGH,
            )
        manager.createNotificationChannel(channel)
    }

    fun notify(
        title: String,
        body: String?,
        navigatePath: String?,
    ) {
        val id = nextId.getAndIncrement()
        val builder =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body.orEmpty())
                .setAutoCancel(true)
                .setDefaults(NotificationCompat.DEFAULT_ALL)

        if (navigatePath != null && navigatePath.startsWith("/")) {
            builder.setContentIntent(activationIntent(navigatePath, id))
        }

        post(id, builder.build())
    }

    /**
     * Android has no universal numeric icon badge. We attach the count to a
     * lightweight summary notification via `setNumber()` (surfaced by some
     * launchers; AOSP shows only a dot). A count of 0 is a no-op: we never
     * cancel notifications just to clear a badge.
     */
    fun setBadgeCount(count: Int) {
        if (count <= 0) return
        val summary =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(context.getString(R.string.app_name))
                .setContentText(
                    context.resources.getQuantityString(R.plurals.badge_text, count, count),
                ).setNumber(count)
                .setSilent(true)
                .setOngoing(false)
                .build()
        post(BADGE_NOTIFICATION_ID, summary)
    }

    /**
     * Post a notification, tolerating a missing notification grant. The
     * `POST_NOTIFICATIONS` permission is revocable on API 33+, so `notify` can
     * throw `SecurityException` even after `areNotificationsEnabled()` — we drop
     * silently rather than crash.
     */
    private fun post(
        id: Int,
        notification: Notification,
    ) {
        if (!manager.areNotificationsEnabled()) return
        try {
            manager.notify(id, notification)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS not granted — drop; web falls back.
        }
    }

    // requestCode is the notification's own id, so each notification gets a
    // distinct PendingIntent — otherwise FLAG_UPDATE_CURRENT would let two paths
    // with colliding hashes overwrite each other's extras and mis-route a tap.
    private fun activationIntent(
        navigatePath: String,
        requestCode: Int,
    ): PendingIntent {
        val intent =
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_NAVIGATE_PATH, navigatePath)
            }
        return PendingIntent.getActivity(
            context,
            requestCode,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    companion object {
        const val EXTRA_NAVIGATE_PATH = "ai.omnigent.android.NAVIGATE_PATH"
        private const val CHANNEL_ID = "omnigent.sessions"
        private const val BADGE_NOTIFICATION_ID = 1
    }
}
