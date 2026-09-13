package ai.omnigent.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
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

    // Last badge state from the web layer, kept so a grant of the API 33+
    // notification permission can replay a badge that was computed (and
    // deduped web-side) while the permission dialog was still open.
    private data class BadgeState(
        val count: Int,
        val navigatePath: String?,
        val title: String?,
        val body: String?,
    )

    @Volatile
    private var lastBadge: BadgeState? = null

    init {
        ensureChannel()
    }

    /**
     * Create the notification channel, idempotently. `createNotificationChannel`
     * is a no-op when the channel already exists, so this is cheap to call
     * repeatedly. Exposed (and re-run from the constructor) so a WorkManager-only
     * process — one that spawned in the background with no Activity ever
     * on-screen — still has the channel before it posts; otherwise an O+ post to
     * a missing channel is silently dropped. A no-op on pre-O.
     */
    fun ensureChannel() {
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
        // A caller-supplied STABLE id (e.g. per-session, via notificationIdFor)
        // so a re-fire updates the same notification instead of stacking, and
        // distinct subjects never collide. When null, an incrementing per-
        // instance id is used — fine for one-off in-app toasts, but NOT for the
        // background worker, which builds a fresh manager each run (the counter
        // would restart and collide across runs).
        notificationId: Int? = null,
        // Optional notification TAG (e.g. the session id). Distinct session ids
        // can share a String.hashCode-derived numeric id ("Aa"/"BB"), so tagging
        // with the full session id is what actually guarantees distinctness —
        // the numeric id stays stable so a re-fire still updates in place.
        tag: String? = null,
    ) {
        val id = notificationId ?: nextId.getAndIncrement()
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

        post(id, builder.build(), tag)
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
        lastBadge = BadgeState(count, navigatePath, title, body)
        if (count <= 0) {
            manager.cancel(BADGE_NOTIFICATION_ID)
            return
        }
        val builder =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title ?: context.getString(R.string.app_name))
                .setContentText(
                    body ?: context.resources.getQuantityString(R.plurals.badge_text, count, count),
                ).setNumber(count)
                .setSilent(true)
                .setOngoing(false)
        if (navigatePath != null && navigatePath.startsWith("/")) {
            // Tap opens the app and routes. Deliberately NOT setAutoCancel: this
            // is an ambient count, not a one-off event — clearing it on tap would
            // drop the only Android count surface while sessions are still
            // pending, and a later poll with the same count won't repost it.
            builder.setContentIntent(activationIntent(navigatePath, BADGE_NOTIFICATION_ID))
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
        setBadgeCount(badge.count, badge.navigatePath, badge.title, badge.body)
    }

    /**
     * Cancel every notification this app has posted. Called on a fresh login so
     * per-session notifications (id >= 2) left over from the PRIOR account don't
     * linger — they'd expose the old account's titles and deep-link into its
     * session ids. The badge (id 1) is withdrawn too; the web layer re-pushes
     * the current count on its next tick, so it self-heals for the new account.
     */
    fun cancelAll() {
        manager.cancelAll()
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
        tag: String? = null,
    ) {
        if (!manager.areNotificationsEnabled()) return
        try {
            // Tag-qualified post: (tag, id) is the notification key, so two
            // sessions whose hash-derived numeric ids collide still coexist.
            // cancelAll() (the only cancel path for tagged posts) cancels
            // tagged and untagged notifications alike.
            manager.notify(tag, id, notification)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS not granted — drop; web falls back.
        }
    }

    // requestCode is the notification's own id AND the intent carries a unique
    // data Uri derived from navigatePath: PendingIntents are keyed by
    // (requestCode, Intent.filterEquals), and filterEquals ignores extras — so
    // two paths whose hash-derived ids collide would otherwise share one
    // PendingIntent, FLAG_UPDATE_CURRENT would overwrite its extras, and both
    // taps would mis-route to the later path. The data Uri makes the intents
    // non-filterEqual even on an id collision.
    private fun activationIntent(
        navigatePath: String,
        requestCode: Int,
    ): PendingIntent {
        val intent =
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
                data = Uri.parse("omnigent://notification$navigatePath")
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
