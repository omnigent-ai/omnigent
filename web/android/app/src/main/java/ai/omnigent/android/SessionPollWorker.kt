package ai.omnigent.android

import android.content.Context
import android.webkit.CookieManager
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

/**
 * Background session poller. Fires local notifications when a session finishes
 * (`running` → `idle`/`failed`) or gains a new pending elicitation, without the
 * app being foregrounded — an interim, OS-scheduled mirror of the web SPA's
 * `useIdleNotifications` hook, driven by WorkManager instead of a live poll.
 *
 * Client-only: it reuses the JWT the shell already injected into the WebView's
 * [CookieManager] (see MainActivity.onSessionToken) rather than duplicating
 * credential storage, and hits the existing `GET /v1/sessions` endpoint. No
 * server change.
 *
 * The 15-minute WorkManager floor doubles as the SPA's 10s idle-settle window:
 * a session seen `running` a poll ago and terminal now is genuinely finished,
 * so unlike the live hook this worker needs no in-worker deferral.
 *
 * Order within a successful poll is detect → notify → save (see doWork): a
 * mid-run process kill after notifying must not advance the snapshot, or the
 * finish would be silently missed. The snapshot is MERGED, not replaced, so a
 * `running` session that scrolls off the fixed top-window keeps its prior
 * status until observed terminal. Posting goes through
 * [NativeNotificationManager], whose constructor (re-)creates the channel so a
 * cold background process still has it, and which no-ops when the
 * POST_NOTIFICATIONS grant is missing.
 *
 * Graceful no-ops (always [Result.success], never a crash or retry storm):
 *   * not logged in / cookie expired → no session cookie → nothing to poll
 *   * no pinned server → nothing to poll
 *   * network/HTTP error → skip this run, prior snapshot untouched, retry next tick
 */
class SessionPollWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result =
        withContext(Dispatchers.IO) {
            val origin = pinnedOrigin() ?: return@withContext Result.success()

            val secure = origin.startsWith("https://")
            val jwt =
                extractCookieValue(
                    CookieManager.getInstance().getCookie(origin),
                    sessionCookieName(secure),
                )
                    ?: return@withContext Result.success() // not logged in / expired — no-op

            val body = fetchSessions(origin, jwt, secure) ?: return@withContext Result.success()
            val sessions = parseSessionList(body)

            // Cheap multi-account guard: a poll started against account A can
            // land after account B logs in and re-points ServerStore. Re-read the
            // pinned origin right before we act on the fetched data; if it
            // changed mid-poll, bail without notifying or saving so we don't post
            // A's sessions (or persist A's snapshot) into B's session. This is a
            // best-effort recheck, not full account-generation plumbing (see the
            // PR's follow-ups) — a switch in the narrow window after this line is
            // still possible.
            if (pinnedOrigin() != origin) return@withContext Result.success()

            val store = SessionSnapshotStore(applicationContext)
            val previous = store.load()

            // Diff FIRST, then notify, then save (in that order). If the process
            // is killed after notifying but before saving, the next run diffs
            // against the un-advanced snapshot and re-fires — an occasional
            // duplicate beats a silently-missed finish alert. Saving first would
            // turn a mid-run kill into a permanent miss.
            //
            // A first run (empty `previous`) yields no transitions, so nothing
            // fires; we still fall through to save so the next run has a baseline.
            if (previous.isNotEmpty()) {
                // Constructing this ensures the notification channel exists in
                // THIS process — the worker often runs in a cold background
                // process where no Activity created it, and an O+ post to a
                // missing channel is silently dropped. post() itself no-ops when
                // POST_NOTIFICATIONS is not granted, so an ungranted background
                // run never crashes.
                val notifications = NativeNotificationManager(applicationContext)
                // Idle loop then elicitation loop, in that order. A session that
                // both finished and raised a prompt this poll shares one
                // notificationId, so the elicitation post is last-write-wins.
                notify(notifications, detectIdleTransitions(previous, sessions), IDLE_BODY)
                notify(notifications, detectNewElicitations(previous, sessions), ELICITATION_BODY)
            }

            // MERGE rather than replace: the list endpoint returns only the top
            // window (limit=20, no `since`), so a `running` session can scroll
            // off between polls. A full-replace would drop its prior `running`
            // status, and its later `running` → terminal finish (observed
            // off-window, or in a poll whose window happens to exclude it) would
            // be missed. Carrying prior off-window entries forward preserves that
            // edge until we actually see the session terminal. An empty parse
            // (no sessions, or a transient all-invalid body) therefore just
            // carries the prior snapshot forward unchanged — no state lost.
            store.save(mergeSnapshot(previous, sessions))
            Result.success()
        }

    /** The pinned server's origin, or null when no server is set. */
    private fun pinnedOrigin(): String? =
        ServerStore(applicationContext).let {
            if (it.hasServer()) originOf(it.currentServerUrl()) else null
        }

    /**
     * Post one notification per session in [sessions] with [body], skipping
     * dead-runner sessions. Each uses a stable per-session [notificationIdFor]
     * id (a fresh manager is built each run, so an incrementing counter would
     * restart and let a later session's finish replace an earlier one's still-
     * undismissed notification), deep-linking to the session.
     */
    private fun notify(
        notifications: NativeNotificationManager,
        sessions: List<SessionState>,
        body: String,
    ) {
        for (session in sessions) {
            if (session.runnerOnline == false) continue // stale dead-runner reconciliation
            notifications.notify(
                title = sessionDisplayLabel(session.title),
                body = body,
                navigatePath = "/c/${session.id}",
                notificationId = notificationIdFor(session.id),
            )
        }
    }

    /**
     * GET the sessions list, authenticating with the reused JWT as both a
     * `Cookie` (matching the WebView) and `Authorization: Bearer` (the server
     * accepts either). Returns the body on 200, or null on any non-200 /
     * network error so the run no-ops and retries next tick.
     */
    private fun fetchSessions(
        origin: String,
        jwt: String,
        secure: Boolean,
    ): String? {
        // Build the connection INSIDE the try: a malformed/legacy non-HTTP
        // persisted origin makes URL(...)/openConnection()/the cast throw, and
        // that must no-op like every other error path rather than fail the run.
        var conn: HttpURLConnection? = null
        return try {
            conn = URL(origin + SESSIONS_LIST_PATH).openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            // This request carries the session JWT (Cookie + Bearer). Do NOT
            // auto-follow redirects: a cross-origin or HTTPS→HTTP-downgrade 3xx
            // would otherwise resend the credential off the pinned origin. A 3xx
            // now surfaces as a non-200 responseCode and is treated as a no-op.
            conn.instanceFollowRedirects = false
            conn.setRequestProperty("Cookie", "${sessionCookieName(secure)}=$jwt")
            conn.setRequestProperty("Authorization", "Bearer $jwt")
            conn.setRequestProperty("Accept", "application/json")
            conn.connectTimeout = HTTP_TIMEOUT_MS
            conn.readTimeout = HTTP_TIMEOUT_MS
            if (conn.responseCode != 200) return null
            // Cheap size cap: the endpoint is limit=20 so a well-behaved body is
            // small, but readText() is otherwise unbounded. Read in fixed chunks
            // into a StringBuilder that grows to the actual body size, capped at
            // MAX_RESPONSE_BYTES; a larger body is treated as a no-op (null)
            // rather than buffered whole. Sizing the buffer to the response (a
            // sane page is a few KB) avoids allocating the full cap every poll.
            conn.inputStream.bufferedReader().use { reader ->
                val chunk = CharArray(READ_CHUNK_CHARS)
                val out = StringBuilder()
                while (true) {
                    val n = reader.read(chunk)
                    if (n < 0) break
                    // Appending this chunk would exceed the cap → oversized, bail.
                    if (out.length + n > MAX_RESPONSE_BYTES) return null
                    out.append(chunk, 0, n)
                }
                out.toString()
            }
        } catch (_: Throwable) {
            null
        } finally {
            conn?.disconnect()
        }
    }

    companion object {
        // Mirrors useIdleNotifications' IDLE_BODY / ELICITATION_BODY.
        const val IDLE_BODY = "Agent finished and is ready for your input."
        const val ELICITATION_BODY = "Agent is asking for your input."
        private const val HTTP_TIMEOUT_MS = 15_000

        // Upper bound on the list-response body we buffer. The endpoint is
        // limit=20, so a sane page is well under this; a larger body is treated
        // as a no-op rather than read whole. ~1M chars.
        private const val MAX_RESPONSE_BYTES = 1_048_576

        // Per-read chunk size. Small and fixed so the buffer footprint tracks
        // the actual (tiny) response rather than the cap above.
        private const val READ_CHUNK_CHARS = 8_192
    }
}
