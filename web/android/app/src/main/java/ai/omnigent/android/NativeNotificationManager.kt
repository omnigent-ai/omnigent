package ai.omnigent.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

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
    notificationOrigin: String?,
) {
    private val manager = NotificationManagerCompat.from(context)

    // Persist allocation so a new Activity or process cannot reuse an unread
    // notification's id.
    private val notificationIds =
        context.getSharedPreferences(NOTIFICATION_PREFS, Context.MODE_PRIVATE)

    @Volatile
    private var notificationOrigin = notificationOrigin

    // Last badge state from the web layer, kept so a grant of the API 33+
    // notification permission can replay a badge that was computed (and
    // deduped web-side) while the permission dialog was still open.
    private data class BadgeState(
        val count: Int,
        val navigatePath: String?,
        val title: String?,
        val body: String?,
        val origin: String?,
    )

    @Volatile
    private var lastBadge: BadgeState? = null

    init {
        val channel =
            NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_HIGH,
            )
        manager.createNotificationChannel(channel)
        claimNotificationOrigin(notificationOrigin)
    }

    fun notify(
        title: String,
        body: String?,
        navigatePath: String?,
    ) {
        val id = nextNotificationId()
        val builder =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body.orEmpty())
                .setAutoCancel(true)
                .setDefaults(NotificationCompat.DEFAULT_ALL)

        if (navigatePath != null && navigatePath.startsWith("/")) {
            notificationOrigin?.let { origin ->
                builder.setContentIntent(activationIntent(navigatePath, id, origin))
            }
        }

        post(id, builder.build())
    }

    /**
     * Android has no universal numeric icon badge, so the count is surfaced as a
     * lightweight summary notification (its `setNumber()` is shown by some
     * launchers; AOSP shows only a dot). Because that notification is often the
     * ONLY thing the user sees, it must be actionable and descriptive: when the
     * web layer supplies a [navigatePath] the tap opens the app and routes there
     * (one waiting session → that session; several → the inbox), and [title] /
     * [body] describe what's waiting instead of a bare "N pending". Older web
     * builds omit these, so we fall back to the app name + "N pending" and no
     * tap intent — the prior behavior.
     *
     * A count of 0 withdraws the summary: the badge notification is the count
     * surface, so once nothing is pending it must not linger as a stale,
     * still-tappable "N sessions need your attention" routing to resolved work.
     */
    fun setBadgeCount(
        count: Int,
        navigatePath: String? = null,
        title: String? = null,
        body: String? = null,
    ) {
        val badge = BadgeState(count, navigatePath, title, body, notificationOrigin)
        lastBadge = badge
        postBadge(badge)
    }

    private fun postBadge(badge: BadgeState) {
        if (badge.count <= 0) {
            manager.cancel(BADGE_NOTIFICATION_ID)
            return
        }
        val builder =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(badge.title ?: context.getString(R.string.app_name))
                .setContentText(
                    badge.body
                        ?: context.resources.getQuantityString(
                            R.plurals.badge_text,
                            badge.count,
                            badge.count,
                        ),
                ).setNumber(badge.count)
                .setSilent(true)
                .setOngoing(false)
        if (badge.navigatePath != null && badge.navigatePath.startsWith("/")) {
            // Tap opens the app and routes. Deliberately NOT setAutoCancel: this
            // is an ambient count, not a one-off event — clearing it on tap would
            // drop the only Android count surface while sessions are still
            // pending, and a later poll with the same count won't repost it.
            badge.origin?.let { origin ->
                builder.setContentIntent(
                    activationIntent(badge.navigatePath, BADGE_NOTIFICATION_ID, origin),
                )
            }
        }
        post(BADGE_NOTIFICATION_ID, builder.build())
    }

    /**
     * Re-post the last badge the web layer sent. Called when the user grants
     * the notification permission: a badge posted before the grant was
     * silently dropped, and the web side won't resend an unchanged state.
     */
    fun replayBadge() {
        val badge = lastBadge ?: return
        postBadge(badge)
    }

    fun setOrigin(origin: String?) {
        notificationOrigin = origin
        rememberNotificationOrigin(origin)
    }

    fun cancelAll() {
        lastBadge = null
        cancelSessionNotifications()
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
        origin: String,
    ): PendingIntent {
        val intent =
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                putExtra(EXTRA_NAVIGATE_PATH, navigatePath)
                putExtra(EXTRA_NOTIFICATION_ORIGIN, origin)
            }
        return PendingIntent.getActivity(
            context,
            requestCode,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun nextNotificationId(): Int =
        synchronized(ID_LOCK) {
            val storedId =
                notificationIds.getInt(KEY_NEXT_NOTIFICATION_ID, FIRST_NOTIFICATION_ID)
            val id = storedId.takeIf { it >= FIRST_NOTIFICATION_ID } ?: FIRST_NOTIFICATION_ID
            // apply(), not commit(): this runs on the UI thread with every
            // notification, and the in-memory value is updated synchronously.
            notificationIds.edit().putInt(KEY_NEXT_NOTIFICATION_ID, id + 1).apply()
            id
        }

    private fun claimNotificationOrigin(origin: String?) {
        synchronized(ID_LOCK) {
            if (notificationIds.contains(KEY_LAST_PINNED_ORIGIN) &&
                notificationIds.getString(KEY_LAST_PINNED_ORIGIN, null) != origin
            ) {
                cancelSessionNotifications()
            }
            rememberNotificationOriginLocked(origin)
        }
    }

    private fun rememberNotificationOrigin(origin: String?) {
        synchronized(ID_LOCK) { rememberNotificationOriginLocked(origin) }
    }

    private fun rememberNotificationOriginLocked(origin: String?) {
        val editor = notificationIds.edit()
        if (origin == null) {
            editor.remove(KEY_LAST_PINNED_ORIGIN)
        } else {
            editor.putString(KEY_LAST_PINNED_ORIGIN, origin)
        }
        editor.commit()
    }

    private fun cancelSessionNotifications() {
        val platformManager =
            context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        // Session notifications are untagged; download completion uses a dedicated tag.
        platformManager.activeNotifications
            .filter { notification -> notification.tag == null }
            .forEach { notification -> manager.cancel(notification.id) }
    }

    companion object {
        const val EXTRA_NAVIGATE_PATH = "ai.omnigent.android.NAVIGATE_PATH"
        const val EXTRA_NOTIFICATION_ORIGIN = "ai.omnigent.android.NOTIFICATION_ORIGIN"
        private const val CHANNEL_ID = "omnigent.sessions"
        private const val BADGE_NOTIFICATION_ID = 1
        private const val FIRST_NOTIFICATION_ID = BADGE_NOTIFICATION_ID + 1
        private const val NOTIFICATION_PREFS = "ai.omnigent.android.notifications"
        private const val KEY_NEXT_NOTIFICATION_ID = "next_notification_id"
        private const val KEY_LAST_PINNED_ORIGIN = "last_pinned_origin"
        private val ID_LOCK = Any()
    }
}
